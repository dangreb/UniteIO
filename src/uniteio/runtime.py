"""Applications, their loop threads, and the runtime that owns them.

A :class:`Runtime` owns a shared executor, a registry of running applications,
and one thread per application. Each :class:`App` runs its own event loop on
that thread. The design decisions behind this module are recorded in
``design/0001-runtime.md``.

Example:
    Start an application, look it up, and close everything::

        import asyncio
        from uniteio import App, Runtime

        class TradeStream(App):
            name = "trades"

            def __init__(self, state):
                self.state = state

            async def __call__(self):
                await asyncio.Event().wait()

        with Runtime() as rt:
            trades = rt.start(TradeStream(state={}))
            assert rt[TradeStream] is trades
            assert rt.get("trades") is trades
"""

from __future__ import annotations

import os
import sys
import asyncio
import inspect

from abc import ABCMeta, abstractmethod
from itertools import count
from contextvars import Context
from collections.abc import Coroutine, Iterable
from threading import Thread, Event, Lock, RLock, current_thread, get_ident
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Any, Callable, ClassVar, Self


__all__ = ["App", "Runtime"]

_DEFAULT = object()
_LOOP_FACTOR = 4  # provisório: o valor final sai da medição (design/0001, "Em aberto")



class App(metaclass=ABCMeta):
    """An asyncio application owning one event loop on one runtime thread.

    The constructor is an ordinary constructor: it only configures, runs on the
    caller's thread, and accepts any signature. Subclasses are not required to
    call ``super().__init__()``. :meth:`__call__` is the initialization and
    service entry point, executed on the application's own loop after
    :meth:`Runtime.start`.

    Runtime attributes are ``None`` until :meth:`Runtime.start` assigns them;
    methods that need a started application raise ``RuntimeError`` before that.

    Attributes:
        name: Registry name. Resolved by :meth:`Runtime.start` from its
            ``name`` argument, a ``name`` class attribute, or the class name.
    """

    name: str | None = None
    loop: asyncio.AbstractEventLoop | None = None
    runtime: Runtime | None = None

    _thread: Thread | None = None
    _done: asyncio.Future | None = None
    _stopped: Event | None = None
    _inflight: set[LoopFuture] | None = None  # submitted loop work not yet settled
    _inflight_lock: Lock | None = None
    _closing: bool = False

    @abstractmethod
    async def __call__(self) -> None:
        """Initialize and serve on the application's loop."""
        ...

    @property
    def thread(self) -> Thread | None:
        return self._thread

    @property
    def started(self) -> bool:
        return self.runtime is not None

    def exception_handler(self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """Receive every unhandled error of this application's loop.

        Covers failing callbacks, protocol callbacks, unretrieved task
        exceptions, and a failing :meth:`__call__`. Override to route errors;
        the default delegates to asyncio's standard reporting.
        """
        loop.default_exception_handler(context)

    def submit(self, target: Coroutine | Iterable[Coroutine] | Callable, /, *args: Any,
               name: str | None = None, context: Context | None = None, eager: bool = False,
               **kwargs: Any) -> Future:
        """Run work for this application from any thread.

        This is the thread boundary of the application: the one way in for work
        coming from other threads. Inside the loop, use ``asyncio`` directly
        (``create_task``, ``TaskGroup``, ``await``).

        Routing:
            - A coroutine object, or a coroutine function called with
              ``*args``/``**kwargs``, runs as a task on this application's loop.
            - An iterable of coroutine objects runs as one ``TaskGroup`` on the
              loop. The future resolves to an insertion-ordered mapping from
              each coroutine object to its result; failures arrive as an
              ``ExceptionGroup`` after the siblings are cancelled.
            - Any other callable runs on the runtime's shared executor.

        Args:
            target: The work, as described above.
            name: Task name. Batch children are named ``"<name>:<index>"``.
            context: ``contextvars.Context`` the task runs in.
            eager: Start the task eagerly. The first step runs on the loop
                thread, inside the callback that creates the task.

        ``name``, ``context`` and ``eager`` belong to ``submit`` and are never
        forwarded to the target; bind such arguments with
        ``functools.partial``, or pass a coroutine object.

        Returns:
            A ``concurrent.futures.Future``. For loop work, blocking on it from
            this application's own thread raises ``RuntimeError`` instead of
            deadlocking; await the work there, or ``asyncio.wrap_future`` it
            from another application's loop. Cancelling it cancels the task.

        Raises:
            RuntimeError: If the application was not started or is stopping.
            TypeError: If ``target`` is unsupported, a batch holds a
                non-coroutine, or task options are given for executor work.
        """
        self._require_started()
        options = {key: value for key, value in (("name", name), ("context", context)) if value is not None}
        if eager:
            options["eager_start"] = True

        if asyncio.iscoroutine(target):
            if args or kwargs:
                target.close()
                raise TypeError("arguments cannot be passed with a coroutine object")
            return self._to_loop(target, options)
        if inspect.iscoroutinefunction(target):
            return self._to_loop(target(*args, **kwargs), options)
        if callable(target):
            if options:
                raise TypeError("name, context and eager apply to loop work only; "
                                "use functools.partial to pass them to an executor callable")
            return self.runtime.executor.submit(target, *args, **kwargs)
        if isinstance(target, Iterable) and not isinstance(target, (str, bytes)):
            batch = tuple(target)
            if args or kwargs or not all(asyncio.iscoroutine(coro) for coro in batch):
                for coro in batch:
                    if asyncio.iscoroutine(coro):
                        coro.close()  # never awaited: close instead of leaving a warning behind
                raise TypeError("a batch must be an iterable of coroutine objects, without arguments")
            return self._to_loop(_run_group(batch, options), options)
        raise TypeError("target must be a coroutine, coroutine function, iterable of coroutines or callable")

    def stop(self, wait: bool = True, timeout: float | None = None) -> bool:
        """Stop this application's loop.

        Returns ``True`` once teardown has completed. A call made from the
        application's own thread only requests the stop and returns ``False``.

        Raises:
            RuntimeError: If the application was not started.
        """
        self._require_started()
        if self._stopped.is_set():
            return True
        if current_thread() is self._thread:
            self._request_stop()
            return False
        try:
            self.loop.call_soon_threadsafe(self._request_stop)
        except RuntimeError:
            pass  # loop already closed
        if wait:
            self._thread.join(timeout)
        return self._stopped.is_set()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the application's thread, like :meth:`Thread.join`.

        Raises:
            RuntimeError: If the application was not started.
        """
        self._require_started()
        self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        """Whether the application's thread is running. ``False`` before start."""
        return self._thread is not None and self._thread.is_alive()

    def _require_started(self) -> None:
        if not self.started:
            raise RuntimeError(f"{self.name or type(self).__name__!r} was not started")

    def _request_stop(self) -> None:
        if not self._done.done():
            self._done.set_result(None)

    def _to_loop(self, coro: Coroutine, options: dict[str, Any]) -> LoopFuture:
        """Hand ``coro`` to the loop thread; the task is created there."""
        outer = LoopFuture(self._thread)
        outer.add_done_callback(self._forget)
        with self._inflight_lock:
            if self._closing:
                coro.close()
                raise RuntimeError(f"{self.name!r} is stopping")
            self._inflight.add(outer)
            # Under the lock, so teardown cannot close the loop in between.
            self.loop.call_soon_threadsafe(self._spawn, coro, outer, options)
        return outer

    def _spawn(self, coro: Coroutine, outer: LoopFuture, options: dict[str, Any]) -> None:
        """Loop thread: create the task and chain it to ``outer``."""
        if outer.cancelled() or self._closing or self._done.done():
            coro.close()
            outer.cancel()
            return
        try:
            task = self.loop.create_task(coro, **options)  # eager tasks take their first step here
        except BaseException as error:
            coro.close()
            if outer.set_running_or_notify_cancel():
                outer.set_exception(error)
            return
        task.add_done_callback(lambda done: _settle(outer, done))
        outer.add_done_callback(lambda future: future.cancelled() and self._cancel_soon(task))

    def _cancel_soon(self, task: asyncio.Task) -> None:
        try:
            self.loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            pass  # loop already closed: the task was cancelled by teardown

    def _forget(self, future: LoopFuture) -> None:
        with self._inflight_lock:
            self._inflight.discard(future)



class BorrowedExecutor(ThreadPoolExecutor):
    """Loop-facing view of the shared executor that a loop cannot shut down.

    ``BaseEventLoop.close()`` shuts down the loop's default executor, and
    ``set_default_executor`` only accepts ``ThreadPoolExecutor`` instances.
    Delegating through this subclass keeps the shared executor alive without
    touching private loop attributes. No worker is ever created by this view.
    """

    def __init__(self, target: ThreadPoolExecutor):
        super().__init__(max_workers=1)
        self._target = target

    def submit(self, fn, /, *args, **kwargs) -> Future:
        return self._target.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        pass



class GuardedLoop(asyncio.EventLoop):
    """The platform's default loop, rejecting non-thread-safe calls from other threads.

    asyncio only checks this in debug mode; elsewhere a ``call_soon`` from the
    wrong thread may sit unnoticed until something else wakes the loop, and
    under free threading it races with the loop's own bookkeeping. The guard
    turns that silent misuse into an immediate error. Behaviour on the owning
    thread is unchanged.

    Tasks created without a name are named ``"<prefix>:<n>"`` (the
    application's name and a per-loop counter), so logs and tracebacks show
    which application a task belongs to. Explicit names are kept as given.
    """

    def __init__(self, prefix: str):
        super().__init__()
        self._uio_owner = get_ident()  # loops are created on the thread that runs them
        self._uio_prefix = prefix
        self._uio_counter = count(1)  # only advanced on the owning thread, after the guard

    def _uio_guard(self, operation: str) -> None:
        if get_ident() != self._uio_owner:
            raise RuntimeError(
                f"loop.{operation}() called from another thread. From outside the loop, "
                "use App.submit() or loop.call_soon_threadsafe()."
            )

    def call_soon(self, callback, *args, context=None):
        self._uio_guard("call_soon")
        return super().call_soon(callback, *args, context=context)

    def call_later(self, delay, callback, *args, context=None):
        self._uio_guard("call_later")
        return super().call_later(delay, callback, *args, context=context)

    def call_at(self, when, callback, *args, context=None):
        self._uio_guard("call_at")
        return super().call_at(when, callback, *args, context=context)

    def create_task(self, coro, **kwargs):
        self._uio_guard("create_task")
        if kwargs.get("name") is None:
            kwargs["name"] = f"{self._uio_prefix}:{next(self._uio_counter)}"
        return super().create_task(coro, **kwargs)



class LoopFuture(Future):
    """Future of loop work that refuses to block the loop it is waiting for.

    Waiting on it from the loop's own thread can never finish, because the
    loop is the one that has to resolve it. That case raises instead.
    """

    def __init__(self, loop_thread: Thread):
        super().__init__()
        self._loop_thread = loop_thread

    def _check_wait(self) -> None:
        if not self.done() and current_thread() is self._loop_thread:
            raise RuntimeError(
                "blocking on loop work from the loop's own thread would deadlock; "
                "await it instead (asyncio.wrap_future)"
            )

    def result(self, timeout: float | None = None) -> Any:
        self._check_wait()
        return super().result(timeout)

    def exception(self, timeout: float | None = None) -> BaseException | None:
        self._check_wait()
        return super().exception(timeout)


def _settle(outer: LoopFuture, task: asyncio.Task) -> None:
    """Copy the outcome of ``task`` into ``outer`` (loop thread)."""
    if outer.cancelled():
        return
    if task.cancelled():
        outer.cancel()
        return
    if not outer.set_running_or_notify_cancel():
        return  # cancelled from another thread meanwhile
    if (error := task.exception()) is not None:
        outer.set_exception(error)
    else:
        outer.set_result(task.result())


async def _run_group(batch: tuple[Coroutine, ...], options: dict[str, Any]) -> dict[Coroutine, Any]:
    """Run a batch of coroutines as one ``TaskGroup``, keyed by coroutine object."""
    name = options.get("name")
    async with asyncio.TaskGroup() as group:
        tasks = {
            coro: group.create_task(coro, **(options | ({"name": f"{name}:{index}"} if name else {})))
            for index, coro in enumerate(batch)
        }
    return {coro: task.result() for coro, task in tasks.items()}



class Runtime:
    """Owner of the shared executor, the application registry and their threads.

    Args:
        max_loops: Maximum number of simultaneously running applications.
            Defaults to a generous multiple of ``os.process_cpu_count()``, which
            honours ``PYTHON_CPU_COUNT`` and ``-X cpu_count``. ``None`` disables
            the limit.
        max_workers: Shared executor size, with ``ThreadPoolExecutor``'s
            default when ``None``.
    """

    _default: ClassVar[Runtime | None] = None
    _default_lock: ClassVar[RLock] = RLock()

    def __init__(self, *, max_loops: int | None = _DEFAULT, max_workers: int | None = None):
        if max_loops is _DEFAULT:
            max_loops = _LOOP_FACTOR * (os.process_cpu_count() or 1)
        self.max_loops = max_loops
        self.gil_enabled = getattr(sys, "_is_gil_enabled", lambda: True)()
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="UIO")
        self._borrowed = BorrowedExecutor(self.executor)
        self._apps: dict[str, App] = {}
        self._lock = RLock()
        self._closed = False

    @classmethod
    def default(cls) -> Runtime:
        """Return the process-wide runtime, creating a new one after close."""
        with cls._default_lock:
            if cls._default is None or cls._default._closed:
                cls._default = cls()
            return cls._default

    # --- context manager -------------------------------------------------------------------

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()  # an error raised here is chained to the one propagating

    # --- registry --------------------------------------------------------------------------

    def __getitem__(self, key: str | type[App]) -> App:
        """Look an application up by name or by class.

        Raises:
            KeyError: If no application matches, or a class matches several.
        """
        with self._lock:
            if isinstance(key, str):
                return self._apps[key]
            found = [app for app in self._apps.values() if isinstance(app, key)]
        if len(found) != 1:
            raise KeyError(f"{len(found)} running applications match {key.__name__}")
        return found[0]

    def get(self, name: str, default: Any = None) -> App | Any:
        with self._lock:
            return self._apps.get(name, default)

    def __contains__(self, app: App) -> bool:
        with self._lock:
            return self._apps.get(app.name) is app

    def __len__(self) -> int:
        with self._lock:
            return len(self._apps)

    # --- lifecycle -------------------------------------------------------------------------

    def start[A: App](self, app: A, *, name: str | None = None) -> A:
        """Start ``app`` on its own loop thread and return it once ready.

        Args:
            app: The application to start.
            name: Registry name. Defaults to ``app.name`` (a class attribute,
                for instance) or the class name. Needed to run several
                instances of one class.

        Raises:
            RuntimeError: If the runtime is closed, the application was already
                started, or the loop budget is exhausted.
            ValueError: If another running application has the same name.
            BaseException: Any error raised while bringing the loop up. Nothing
                stays registered in that case.
        """
        name = name or app.name or type(app).__name__
        with self._lock:
            if self._closed:
                raise RuntimeError("Runtime is closed")
            if app.started:
                raise RuntimeError(f"{app.name!r} was already started")
            if name in self._apps:
                raise ValueError(f"an application named {name!r} is already running")
            if self.max_loops is not None and len(self._apps) >= self.max_loops:
                raise RuntimeError(
                    f"loop budget exhausted ({self.max_loops} running). Loops are topology, "
                    "not a scaling unit: if applications are being created per connection, "
                    "request or user, use tasks inside one application instead. "
                    "Raise Runtime(max_loops=...) if this topology is intended."
                )
            # Reserve the slot; readiness is awaited outside the lock.
            previous_name = vars(app).get("name")
            app.name = name
            app.runtime = self
            app._stopped = Event()
            app._inflight, app._inflight_lock, app._closing = set(), Lock(), False
            app._thread = Thread(target=self._host, args=(app, ready := Future()),
                                 name=f"UIO:{app.name}", daemon=True)
            self._apps[app.name] = app

        app._thread.start()
        try:
            ready.result()
        except BaseException:
            self._discard(app)
            app.runtime = app.loop = app._thread = app._done = app._stopped = None
            app._inflight = app._inflight_lock = None
            if previous_name is None:
                vars(app).pop("name", None)  # fall back to the class attribute again
            else:
                app.name = previous_name
            raise
        return app

    def close(self, timeout: float | None = None) -> None:
        """Stop every application, then shut the shared executor down.

        Stop requests are broadcast before joining, so applications tear down
        concurrently. Idempotent.

        Raises:
            RuntimeError: If called from an application thread of this runtime.
        """
        with self._lock:
            apps = tuple(self._apps.values())
            if any(current_thread() is app._thread for app in apps):
                raise RuntimeError("Runtime.close() cannot be called from an application thread")
            self._closed = True
        for app in apps:
            app.stop(wait=False)
        for app in apps:
            app._thread.join(timeout)
        self.executor.shutdown(wait=True)

    def _discard(self, app: App) -> None:
        with self._lock:
            if self._apps.get(app.name) is app:
                del self._apps[app.name]

    def _host(self, app: App, ready: Future) -> None:
        """Thread target: run ``app``'s loop until stopped, then tear it down."""
        try:
            loop = GuardedLoop(app.name)
        except BaseException as error:
            ready.set_exception(error)
            return

        try:
            loop.set_default_executor(self._borrowed)
            loop.set_exception_handler(app.exception_handler)
            app.loop = loop
            app._done = loop.create_future()

            entry = loop.create_task(app(), name=f"{app.name}:__call__")
            entry.add_done_callback(_report_entry_failure)
            loop.call_soon(ready.set_result, None)
        except BaseException as error:
            loop.close()
            ready.set_exception(error)
            return

        try:
            loop.run_until_complete(app._done)
        finally:
            with app._inflight_lock:
                app._closing = True  # submit() refuses from here on
            try:
                _cancel_pending(loop)
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                # Work submitted but never spawned: its callback died with the loop.
                with app._inflight_lock:
                    abandoned = tuple(app._inflight)
                for future in abandoned:
                    future.cancel()
                self._discard(app)
                app._stopped.set()



def _report_entry_failure(task: asyncio.Task) -> None:
    """Route a failing ``App.__call__`` to the loop's exception handler."""
    if not task.cancelled() and (error := task.exception()) is not None:
        task.get_loop().call_exception_handler({
            "message": f"{task.get_name()} failed",
            "exception": error,
            "task": task,
        })


def _cancel_pending(loop: asyncio.AbstractEventLoop) -> None:
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

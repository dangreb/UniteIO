"""Threaded asyncio applications sharing a process-wide executor.

``UniteIO`` gives each application class one singleton instance, one daemon
thread, and one asyncio event loop. All applications share the singleton
``UIOPool`` for synchronous callable work while coroutine work remains on the
application's own loop.

Example:
    Define, start, submit work to, and stop an application::

        import asyncio
        from uniteio import UniteIO

        class Service(UniteIO, prefix="SVC"):
            async def __call__(self):
                await asyncio.Event().wait()

        service = Service()
        future = service.submit(asyncio.sleep, 0, result="ready")
        assert future.result() == "ready"
        service.stop()

The package targets CPython free-threaded builds. Closing an application loop
uses a CPython-specific executor-detachment step so the shared pool remains
available to other applications.
"""

from __future__ import annotations

import os
import sys
import time
import inspect
import asyncio
import warnings
import itertools

from contextvars import Context
from string import ascii_uppercase
from threading import Thread, Event, RLock, current_thread
from concurrent.futures import ThreadPoolExecutor, Future

from abc import ABCMeta, abstractmethod
from typing import Any, overload, Callable, ClassVar, Coroutine, Optional, Iterable, Self, final

from uniteio.adjustable_pool import AdjustableThreadPoolExecutor

_gil = hasattr(sys, '_is_gil_enabled') and sys._is_gil_enabled()

__all__ = ['UniteIO', 'UIOPool']



class UIOPoolMeta(ABCMeta):
    """Create at most one active :class:`UIOPool` per concrete pool class.

    Construction is protected by a reentrant lock. A pool removes itself from
    the metaclass registry after successful shutdown, allowing a new executor
    to be created later in the same process.
    """

    _instances = {}
    _lock = RLock()

    def __call__(cls, max_workers=None):
        """Create the active pool or return the existing pool.

        Args:
            max_workers: Worker limit used only when constructing the first
                active instance. Later calls return that instance unchanged.

        Returns:
            The active singleton instance of ``cls``.
        """
        with cls._lock:
            if cls not in cls._instances:
                cls._instances[cls] = super(UIOPoolMeta, cls).__call__(max_workers=max_workers)
            return cls._instances[cls]



class UIOArchetype(Thread, metaclass=ABCMeta):
    """Internal interface shared by application thread implementations.

    Concrete archetypes provide an asynchronous entry point and expose the
    event signaling that lets their metaclass wait for loop readiness.
    """

    pool: ThreadPoolExecutor
    loop: asyncio.BaseEventLoop

    _wait: Event
    _kwargs: dict[str, Any]

    def wait(self) -> None:
        """Block until the application's event-loop thread reports readiness."""
        self._wait.wait()

    @abstractmethod
    async def __call__(self) -> None:
        """Run the application's asynchronous entry point."""
        ...



class UIOPool(AdjustableThreadPoolExecutor, metaclass=UIOPoolMeta):
    """Process-wide executor shared by active :class:`UniteIO` applications.

    The pool is a restartable singleton: repeated construction returns the
    active executor, while construction after :meth:`shutdown` creates a new
    executor. Each application still owns a distinct asyncio event loop.

    Args:
        max_workers: Maximum number of executor worker threads. ``None`` uses
            the standard :class:`ThreadPoolExecutor` default.

    Example:
        Submit synchronous work directly to the shared pool::

            from uniteio import UIOPool

            pool = UIOPool(max_workers=4)
            assert pool.submit(sum, [1, 2, 3]).result() == 6
            pool.shutdown()
    """

    _apps: dict[type[UIOArchetype], UIOArchetype]

    def __init__(self, max_workers=None):
        """Initialize the shared executor and application registry.

        Args:
            max_workers: Maximum number of worker threads, or ``None`` to use
                the standard executor default.
        """
        _gil and warnings.warn("UniteIO is inteded to be used with free-threaded interpreter environments.")
        super(UIOPool, self).__init__(max_workers=max_workers, thread_name_prefix="UIO:")
        self._apps = {}
        self._uio_shutdown_started = False
        self._uio_shutdown_complete = Event()

    def __call__(self, app: UIOArchetype) -> UIOArchetype:
        """Register an application with this pool.

        Registration assigns the pool to ``app`` but does not start the app.

        Args:
            app: Application instance to associate with the shared executor.

        Returns:
            The same application instance, enabling ``pool(app).start()``.

        Raises:
            RuntimeError: If the pool is shutting down, has already shut down,
                or already contains an application of the same class.
        """
        with self.__class__._lock:
            if self._uio_shutdown_started or self._shutdown:
                raise RuntimeError("Cannot register an app while UIOPool is shutting down")
            if app.__class__ in self._apps:
                raise RuntimeError(f"App {app.name} is already initialized")
            app.pool = self
            self._apps[app.__class__] = app
            return app

    def discard(self, app: UIOArchetype) -> None:
        """Remove an application without affecting the executor.

        Args:
            app: Application to remove. A stale or unknown instance is ignored.
        """
        with self.__class__._lock:
            if self._apps.get(app.__class__) is app:
                self._apps.pop(app.__class__)

    def shutdown(self, wait=True, *, cancel_futures=False):
        """Stop every application before shutting down the executor.

        Stop requests are broadcast to all registered applications first, then
        every application thread is joined. Only after their loops are closed
        is :class:`ThreadPoolExecutor` shutdown invoked. The pool is removed
        from the singleton registry after successful completion.

        Args:
            wait: Passed to ``ThreadPoolExecutor.shutdown``. Applications are
                always joined first, even when this argument is ``False``.
            cancel_futures: Whether queued executor futures that have not begun
                execution should be cancelled.

        Raises:
            RuntimeError: If called from one of the registered application
                threads, because that thread cannot synchronously join itself.

        Example:
            Stop all applications and replace the exhausted singleton::

                old_pool = UIOPool()
                old_pool.shutdown(cancel_futures=True)
                new_pool = UIOPool()
                assert new_pool is not old_pool
        """
        with self.__class__._lock:
            if self._uio_shutdown_started:
                shutdown_complete = self._uio_shutdown_complete
                shutdown_owner = False
                apps = ()
            else:
                apps = tuple(self._apps.values())
                if current_thread() in apps:
                    raise RuntimeError(
                        "UIOPool.shutdown() cannot be called from a running app thread"
                    )
                self._uio_shutdown_started = True
                shutdown_complete = self._uio_shutdown_complete = Event()
                shutdown_owner = True

        if not shutdown_owner:
            if wait:
                shutdown_complete.wait()
            return

        try:
            # Request every loop to stop before waiting on any individual app.
            # This lets all applications tear down concurrently.
            for app in apps:
                app.stop(wait=False)
            for app in apps:
                app.join()

            super(UIOPool, self).shutdown(
                wait=wait,
                cancel_futures=cancel_futures,
            )
        except BaseException:
            with self.__class__._lock:
                self._uio_shutdown_started = False
            raise
        else:
            with self.__class__._lock:
                self._apps.clear()
                if self.__class__._instances.get(self.__class__) is self:
                    self.__class__._instances.pop(self.__class__)
        finally:
            shutdown_complete.set()



class UIOTask(asyncio.Task):
    """Internal asyncio task carrying metadata supplied by ``UniteIO.submit``.

    ``run_coroutine_threadsafe`` does not expose every modern ``Task`` option.
    ``UIOTask`` therefore uses a short-lived coroutine-to-options registry to
    transfer names, contexts, and eager-start settings into the loop's task
    factory. Application code normally does not instantiate this class.
    """

    _coroarg: dict[Coroutine, dict] = {}

    @classmethod
    def coroargs(cls, coro: Coroutine, **kwargs):
        """Associate task-construction arguments with a coroutine object.

        Args:
            coro: Coroutine that will shortly be scheduled on a UniteIO loop.
            **kwargs: Keyword arguments to pass to :class:`asyncio.Task`, such
                as ``name``, ``context``, or ``eager_start``.
        """
        cls._coroarg.setdefault(coro, kwargs)

    def __init__(self, loop, coro, *, name=None, context=None, eager_start=False, **kwargs):
        """Create a task using factory arguments and registered metadata.

        Args:
            loop: Event loop that owns the task.
            coro: Coroutine executed by the task.
            name: Optional name supplied directly by the loop task factory.
            context: Optional :class:`contextvars.Context` for execution.
            eager_start: Whether execution may start eagerly when supported by
                the running Python version.
            **kwargs: Additional task-constructor options supplied by asyncio.
        """
        uio: UniteIO = getattr(loop, "uio", None)

        cargs = self.__class__._coroarg.pop(coro, {})

        if name is not None:
            cargs["name"] = name
        else:
            cargs.setdefault("name", uio.taskname())

        if context is not None:
            cargs["context"] = context

        if eager_start:
            cargs["eager_start"] = True
        else:
            cargs.setdefault("eager_start", False)

        cargs.update(kwargs)
        super(UIOTask, self).__init__(coro, loop=loop, **cargs)



class UIOMeta(ABCMeta):
    """Create one active application instance per :class:`UniteIO` subclass.

    The metaclass constructs the application, registers it with ``UIOPool``,
    starts its thread, waits for loop readiness, and schedules its asynchronous
    entry point. Stopped instances are discarded so the subclass can be
    started again.
    """
    _instances: dict[type[UIOArchetype], UIOArchetype] = {}
    _lock = RLock()

    def __call__(cls, **kwargs):
        """Return the active instance of an application subclass.

        Args:
            **kwargs: Arguments forwarded to the subclass constructor when a
                new instance is required. They are ignored when an active
                singleton already exists.

        Returns:
            The active application instance.

        Raises:
            RuntimeError: If the subclass constructor did not initialize the
                :class:`threading.Thread` portion through ``UniteIO.__init__``.
        """
        with cls._lock:
            if cls not in cls._instances:
                instance = super(UIOMeta, cls).__call__(**kwargs)
                if not instance._initialized:
                    raise RuntimeError("Call to UniteIO super constructor is mandatory")
                UIOPool()(instance).start()
                instance.wait()
                asyncio.run_coroutine_threadsafe(instance(), instance.loop)
                cls._instances[cls] = instance
            return cls._instances[cls]

    def discard(cls, instance: UIOArchetype) -> None:
        """Forget a stopped application so its class can be started again.

        Args:
            instance: Application to remove. The operation is ignored when it
                is no longer the registered instance for ``cls``.
        """
        with cls._lock:
            if cls._instances.get(cls) is instance:
                cls._instances.pop(cls)



class UniteIO(UIOArchetype, metaclass=UIOMeta):
    """Base class for singleton applications running dedicated asyncio loops.

    Each concrete subclass owns one active daemon thread and event loop. The
    subclass's asynchronous :meth:`__call__` method is scheduled automatically
    during construction. Synchronous callables submitted through :meth:`submit`
    run on the shared :class:`UIOPool`; coroutines run on the application's
    loop.

    Keyword arguments accepted by a subclass can be forwarded to this base
    constructor. Keys that do not already exist become both attributes and
    item-accessible configuration values.

    Args:
        **kwargs: Application configuration exposed through attributes and
            ``app[key]`` access.

    Example:
        Create an application, submit both kinds of work, and stop it::

            import asyncio
            from uniteio import UniteIO

            class Processor(UniteIO, prefix="PROC"):
                def __init__(self, endpoint):
                    super().__init__(endpoint=endpoint)

                async def __call__(self):
                    await asyncio.Event().wait()

            app = Processor(endpoint="local")
            assert app.endpoint == "local"
            assert app["endpoint"] == "local"
            assert app.submit(sum, [1, 2, 3]).result() == 6
            app.stop()
    """

    pool: UIOPool
    loop: asyncio.BaseEventLoop

    _done: asyncio.Future
    _kwargs: dict[str, Any]

    _except: list
    _prefix: ClassVar[str]
    _count: ClassVar[Callable] = itertools.count(1).__next__

    _appname: ClassVar[str]


    @classmethod
    def __init_subclass__(cls, name=None, prefix=None, **kwargs):
        """Configure application naming and validate its constructor.

        Args:
            name: Human-readable application name used in the thread name.
                Defaults to the subclass name.
            prefix: Prefix used for automatically generated task names.
                Defaults to the uppercase letters in the subclass name, or the
                application name when no uppercase letters are present.
            **kwargs: Reserved subclass configuration keywords.

        Raises:
            RuntimeError: If the subclass constructor accepts positional
                varargs. Application parameters must be explicitly named.

        Example:
            Supply readable thread and task names at class definition time::

                class Worker(UniteIO, name="Image worker", prefix="IMG"):
                    async def __call__(self):
                        ...
        """
        cls._appname = name or cls.__name__
        cls._prefix = prefix or "".join(cl for cl in cls.__name__ if cl in ascii_uppercase) or cls._appname
        """Ensure no varargs"""
        init = cls.__init__
        if init is not object.__init__:
            init_signature = inspect.signature(init)
            # Consider the constructor parameters excluding 'self'
            parameters = [
                p
                for p in init_signature.parameters.values()
                if p.name != "self" and p.kind != p.VAR_KEYWORD
            ]
            for p in parameters:
                if p.kind == p.VAR_POSITIONAL:
                    raise RuntimeError(
                        "Unite IO applications should always "
                        "specify their parameters in the signature"
                        " of their __init__ (no varargs)."
                        " %s with constructor %s doesn't "
                        " follow this convention." % (cls, init_signature)
                    )
        pass


    def taskname(self) -> str:
        """Return a unique task name using the application's prefix.

        Returns:
            A name such as ``"API:3"``.
        """
        return f"{self.__class__._prefix}:{self.__class__._count()}"

    def __init__(self, **kwargs) -> None:
        """Initialize thread state, lifecycle events, and configuration.

        Args:
            **kwargs: Configuration values to expose as attributes and through
                :meth:`__getitem__`. Existing attributes are not overwritten.
        """
        self._except = []
        self._wait = Event()
        self._stopped = Event()
        super(UniteIO, self).__init__(name=f'UIO:{self.__class__._appname}', daemon=True)
        self._kwargs = {a: v for a, v in (kwargs or {}).items() if not hasattr(self, a) and not setattr(self, a, v)}


    def run(self) -> None:
        """Run and ultimately close the application's event loop.

        This is the target executed by :class:`threading.Thread`; callers
        should not invoke it directly. On exit it cancels pending tasks,
        finalizes asynchronous generators, detaches the shared executor, closes
        the loop, and unregisters the application.
        """
        self.loop = asyncio.new_event_loop()
        setattr(self.loop, "uio", self)
        self.loop.set_task_factory(UIOTask)
        # self.loop.set_exception_handler(self.exception_handler)
        self.loop.set_default_executor(self.pool)
        self._done = self.loop.create_future()
        self.loop.call_soon_threadsafe(self._wait.set)
        try:
            self.loop.run_until_complete(future=self._done)
        finally:
            try:
                self._cancel_pending_tasks()
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            finally:
                # BaseEventLoop.close() shuts down its default executor. The
                # executor belongs to UIOPool, not to this application, so
                # detach it before closing this application's loop.
                if getattr(self.loop, "_default_executor", None) is self.pool:
                    self.loop._default_executor = None
                try:
                    self.loop.close()
                finally:
                    self.pool.discard(self)
                    self.__class__.discard(self)
                    self._stopped.set()

    def _cancel_pending_tasks(self) -> None:
        """Cancel and drain every pending task owned by the application loop.

        Task exceptions are collected during teardown so one failed task does
        not prevent remaining tasks from receiving cancellation.
        """
        pending = {
            task for task in asyncio.all_tasks(self.loop)
            if not task.done()
        }
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )


    @abstractmethod
    async def __call__(self) -> None:
        """Implement the application's asynchronous entry point.

        The metaclass schedules this coroutine once after the event loop is
        ready. Long-running applications commonly wait on an event or run a
        service loop until :meth:`stop` cancels the task.

        Example:
            Keep an application alive until shutdown::

                async def __call__(self):
                    await asyncio.Event().wait()
        """
        await super(UniteIO, self).__call__()


    def __getitem__(self, key):
        """Return a constructor configuration value by key.

        Args:
            key: Configuration key forwarded to ``UniteIO.__init__``.

        Returns:
            The stored configuration value.

        Raises:
            KeyError: If no configuration value exists for ``key``.

        Example:
            ``app["endpoint"]`` is equivalent to ``app.endpoint`` for a
            forwarded ``endpoint`` argument.
        """
        return self._kwargs[key]


    def _request_stop(self) -> None:
        """Resolve the loop sentinel from its owning event-loop thread.

        This helper is idempotent and must execute on ``self.loop``.
        """
        if not self._done.done():
            self._done.set_result(None)

    def stop(self, wait: bool = True, timeout: Optional[float] = None) -> bool:
        """Stop this application while leaving the shared executor active.

        When called outside the application thread, ``wait=True`` waits for
        task cancellation, loop closure, and thread termination. Calls made
        by the application itself never block waiting for their own thread.

        Args:
            wait: Whether an external caller should join the application thread
                before returning. Ignored when called by the application itself.
            timeout: Maximum seconds to wait for thread termination, or ``None``
                to wait indefinitely.

        Returns:
            ``True`` when teardown has completed before return; otherwise
            ``False``. A self-stop request returns ``False`` because cleanup
            continues after the current callback yields control.

        Example:
            Request shutdown and wait at most two seconds::

                if not app.stop(timeout=2):
                    print("application is still stopping")
        """
        if self._stopped.is_set():
            return True

        if current_thread() is self:
            self._request_stop()
            return False

        if self.is_alive():
            try:
                self.loop.call_soon_threadsafe(self._request_stop)
            except RuntimeError:
                # The loop may have closed between is_alive() and scheduling.
                pass

        if wait:
            self.join(timeout)
        return self._stopped.is_set()


    async def asleep(self, delay, result=None):
        """Sleep asynchronously on the application's event loop.

        Args:
            delay: Number of seconds to suspend the current coroutine.
            result: Value returned after the delay. A falsey value selects the
                application instance instead.

        Returns:
            ``result`` when truthy, otherwise ``self``.

        Example:
            ``await self.asleep(0.1, "ready")`` returns ``"ready"``.
        """
        return await asyncio.sleep(delay, result or self)

    def sleep(self, delay, result=None):
        """Block the current thread for a specified duration.

        Use this helper only in synchronous work submitted to ``UIOPool``;
        calling it on an event-loop thread blocks that loop.

        Args:
            delay: Number of seconds to block the current thread.
            result: Value returned after the delay. A falsey value selects the
                application instance instead.

        Returns:
            ``result`` when truthy, otherwise ``self``.

        Example:
            ``app.submit(app.sleep, 0.1, "ready").result()`` returns
            ``"ready"``.
        """
        time.sleep(delay)
        return result or self


    def exception_handler(self, loop: asyncio.BaseEventLoop, context) -> None:
        """Record an asyncio exception context and delegate default reporting.

        Args:
            loop: Event loop that reported the exception.
            context: Asyncio exception-handler context dictionary.

        Example:
            Install the handler from :meth:`run` with
            ``loop.set_exception_handler(self.exception_handler)``.
        """
        self._except.append(context)
        loop.default_exception_handler(context)
        pass


    @overload
    def submit(self, target: Coroutine, /, *, name: Optional[str] = None, eager: bool = False) -> Future:
        """Type signature for submitting one pre-created coroutine."""
        ...

    @overload
    def submit(self, target: Iterable[Coroutine], /, *, name: Optional[str] = None, eager: bool = False) -> Future:
        """Type signature for submitting an iterable of coroutines."""
        ...

    def submit(self, target: Callable, /, *args, name: Optional[str] = None, context: Context = None, eager: bool = False, **kwargs) -> Future:
        """Submit coroutine or synchronous work to the appropriate runtime.

        Coroutine objects and coroutine functions execute on this application's
        event loop. An iterable of coroutine objects executes inside an
        :class:`asyncio.TaskGroup`. Other callables execute on the shared
        :class:`UIOPool`.

        Args:
            target: Coroutine object, coroutine function, iterable of coroutine
                objects, or synchronous callable.
            *args: Positional arguments passed to a coroutine function or
                synchronous callable.
            name: Optional asyncio task name. Batch child names append their
                zero-based index, for example ``"batch:0"``.
            context: Optional :class:`contextvars.Context` used for coroutine
                execution. It does not apply to synchronous executor work.
            eager: Request eager task start when supported by asyncio.
            **kwargs: Keyword arguments passed to a coroutine function or
                synchronous callable.

        Returns:
            A :class:`concurrent.futures.Future`. A batch future resolves to an
            insertion-ordered mapping from each original coroutine object to
            its return value.

        Raises:
            TypeError: If ``target`` is unsupported or a batch contains a
                non-coroutine member.
            RuntimeError: If coroutine work is submitted after the application
                event loop has closed, or synchronous work is submitted after
                the shared executor has shut down.

        Example:
            Submit coroutine and synchronous work from another thread::

                async def fetch(identifier):
                    return identifier

                one = app.submit(fetch, 1, name="fetch-one").result()
                many = app.submit(
                    [fetch(2), fetch(3)],
                    name="fetch-batch",
                ).result()
                total = app.submit(sum, [one, *many.values()]).result()
        """
        tname = name or self.taskname()
        if isinstance(target, Iterable):
            _target = tuple(target)
            for idex, coro in enumerate(_target):
                if not asyncio.iscoroutine(coro):
                    raise TypeError("Target must be a coroutine, coroutine function or callable")
                UIOTask.coroargs(coro, name=f"{tname}:{idex}", context=context, eager_start=eager)
            coro = self._task_group_coro(coroset=_target)
        elif asyncio.iscoroutine(target):
            coro = target
        elif inspect.iscoroutinefunction(target):
            coro = target(*args, **kwargs)
        elif callable(target):
            return self.pool.submit(target, *args, **kwargs)
        else:
            raise TypeError("Target must be a coroutine, coroutine function or callable")
        UIOTask.coroargs(coro, name=tname, context=context, eager_start=eager)
        return asyncio.run_coroutine_threadsafe(coro, loop=self.loop)

    @staticmethod
    async def _task_group_coro(coroset: Iterable[Coroutine]):
        """Execute a coroutine collection as one structured task group.

        Args:
            coroset: Re-iterable collection of coroutine objects. ``submit``
                materializes one-shot inputs before calling this helper.

        Returns:
            An insertion-ordered mapping from each coroutine object to its
            completed result.

        Raises:
            ExceptionGroup: If one or more child tasks fail. TaskGroup cancels
                unfinished siblings before propagating the group.
        """
        async with asyncio.TaskGroup() as tg:
            fooret = {co: tg.create_task(co) for co in coroset}
        return {co:ts.result() for co,ts in fooret.items()}

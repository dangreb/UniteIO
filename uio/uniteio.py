

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

from uio.adjustable_pool import AdjustableThreadPoolExecutor

_gil = hasattr(sys, '_is_gil_enabled') and sys._is_gil_enabled()

__all__ = ['UniteIO', 'UIOPool']



class UIOPoolMeta(ABCMeta):
    """Singleton metaclass for UIOPool."""

    _instances = {}
    _lock = RLock()

    def __call__(cls, max_workers=None):
        """Create or return an instance of the class."""
        with cls._lock:
            if cls not in cls._instances:
                cls._instances[cls] = super(UIOPoolMeta, cls).__call__(max_workers=max_workers)
            return cls._instances[cls]



class UIOArchetype(Thread, metaclass=ABCMeta):

    pool: ThreadPoolExecutor
    loop: asyncio.BaseEventLoop

    _wait: Event
    _kwargs: dict[str, Any]

    def wait(self) -> None:
        self._wait.wait()

    @abstractmethod
    async def __call__(self) -> None:...



class UIOPool(AdjustableThreadPoolExecutor, metaclass=UIOPoolMeta):
    """Common thread pool for UniteIO."""

    _apps: dict[type[UIOArchetype], UIOArchetype]

    def __init__(self, max_workers=None):
        """
        Initialize the UIOPool with specified parameters.

        :param max_workers: Maximum number of worker threads in the pool.
        """
        _gil and warnings.warn("UniteIO is inteded to be used with free-threaded interpreter environments.")
        super(UIOPool, self).__init__(max_workers=max_workers, thread_name_prefix="UIO:")
        self._apps = {}
        self._uio_shutdown_started = False
        self._uio_shutdown_complete = Event()

    def __call__(self, app: UIOArchetype) -> UIOArchetype:
        with self.__class__._lock:
            if self._uio_shutdown_started or self._shutdown:
                raise RuntimeError("Cannot register an app while UIOPool is shutting down")
            if app.__class__ in self._apps:
                raise RuntimeError(f"App {app.name} is already initialized")
            app.pool = self
            self._apps[app.__class__] = app
            return app

    def discard(self, app: UIOArchetype) -> None:
        """Remove an application without affecting the executor."""
        with self.__class__._lock:
            if self._apps.get(app.__class__) is app:
                self._apps.pop(app.__class__)

    def shutdown(self, wait=True, *, cancel_futures=False):
        """Stop every application before shutting down the shared executor."""
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

    _coroarg: dict[Coroutine, dict] = {}

    @classmethod
    def coroargs(cls, coro: Coroutine, **kwargs):
        cls._coroarg.setdefault(coro, kwargs)

    def __init__(self, loop, coro, *, name=None, context=None, eager_start=False, **kwargs):
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
    """Singleton metaclass for UniteIO."""
    _instances: dict[type[UIOArchetype], UIOArchetype] = {}
    _lock = RLock()

    def __call__(cls, **kwargs):
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
        """Forget a stopped application so its class can be started again."""
        with cls._lock:
            if cls._instances.get(cls) is instance:
                cls._instances.pop(cls)



class UniteIO(UIOArchetype, metaclass=UIOMeta):
    """UniteIO class for handling I/O operations in a unified manner.

    This class provides a unified interface for performing I/O operations
    asynchronously using asyncio and ThreadPoolExecutor. It allows for
    efficient execution of blocking I/O operations in a separate thread
    pool while keeping the main event loop responsive.
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
        return f"{self.__class__._prefix}:{self.__class__._count()}"

    def __init__(self, **kwargs) -> None:
        """Initialize the UniteIO instance."""
        self._except = []
        self._wait = Event()
        self._stopped = Event()
        super(UniteIO, self).__init__(name=f'UIO:{self.__class__._appname}', daemon=True)
        self._kwargs = {a: v for a, v in (kwargs or {}).items() if not hasattr(self, a) and not setattr(self, a, v)}


    def run(self) -> None:
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
        """Cancel and drain every task owned by this application's loop."""
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
        await super(UniteIO, self).__call__()


    def __getitem__(self, key):
        return self._kwargs[key]


    def _request_stop(self) -> None:
        """Resolve the loop sentinel from its owning event-loop thread."""
        if not self._done.done():
            self._done.set_result(None)

    def stop(self, wait: bool = True, timeout: Optional[float] = None) -> bool:
        """Stop this application while leaving the shared executor active.

        When called outside the application thread, ``wait=True`` waits for
        task cancellation, loop closure, and thread termination. Calls made
        by the application itself never block waiting for their own thread.
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
        """Asynchronously sleep for a specified duration."""
        return await asyncio.sleep(delay, result or self)

    def sleep(self, delay, result=None):
        """Sleep for a specified duration."""
        time.sleep(delay)
        return result or self


    def exception_handler(self, loop: asyncio.BaseEventLoop, context) -> None:
        self._except.append(context)
        loop.default_exception_handler(context)
        pass


    @overload
    def submit(self, target: Coroutine, /, *, name: Optional[str] = None, eager: bool = False) -> Future:
        ...

    @overload
    def submit(self, target: Iterable[Coroutine], /, *, name: Optional[str] = None, eager: bool = False) -> Future:
        ...

    def submit(self, target: Callable, /, *args, name: Optional[str] = None, context: Context = None, eager: bool = False, **kwargs) -> Future:
        """Task submission to the UniteIO application."""
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
        """Convert an iterable of coroutines into a coroutine that waits for all of them."""
        async with asyncio.TaskGroup() as tg:
            fooret = {co: tg.create_task(co) for co in coroset}
        return {co:ts.result() for co,ts in fooret.items()}

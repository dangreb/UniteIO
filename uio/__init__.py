
import os
import sys
import time
import asyncio

from threading import Thread, RLock
from concurrent.futures import ThreadPoolExecutor

from abc import ABCMeta, abstractmethod
from typing import overload, Callable, ClassVar, Coroutine, Optional, Iterable

_gil = hasattr(sys, 'gil') and sys.gil.is_enabled()


__all__ = ['UniteIO', 'UIOPool']




class _PlaceholderLogger:
    def __getattr__(self, item):
        return lambda *args, **kwargs: None



class _PoolMeta(ABCMeta):
    """Singleton metaclass."""
    _instances = {}
    def __call__(cls, **kwargs):
        """Create or return an instance of the class."""
        if cls not in cls._instances:
            cls._instances[cls] = super(_PoolMeta, cls).__call__(**kwargs)

        elif kwargs and any([
            kwargs.get("max_workers", None) not in (None, cls._instances[cls].max_workers),
            kwargs.get("single_loop", None) not in (None, cls._instances[cls].single_loop)
        ]):
            raise ValueError("Cannot change an active pool. Shutdown first.")

        return cls._instances[cls]



class UIOPool(ThreadPoolExecutor, metaclass=_PoolMeta):
    """Common thread pool for UniteIO."""
    _global_loop: Optional[asyncio.AbstractEventLoop]

    def __init__(self, *, max_workers=os.cpu_count()-2, single_loop=_gil):
        """
        Initialize the UIOPool with specified parameters.

        :param max_workers: Maximum number of worker threads in the pool.
        :param single_loop: Whether to use a single event loop for the pool.
        """
        super(UIOPool, self).__init__(max_workers=max_workers)
        self._global_loop = asyncio.new_event_loop() if single_loop else None
        self.single_loop = single_loop
        self.max_workers = max_workers

    def get_loop(self):
        """Get the event loop for the pool."""
        loop = self._global_loop or asyncio.new_event_loop()
        loop.set_default_executor(self)
        return loop

    def shutdown(self, wait = True, *, cancel_futures = True):
        """Shutdown the entire active pool."""
        if not self._shutdown_lock.locked():
            with self._shutdown_lock:
                UniteIO.shutdown(wait)
            super(UIOPool, self).shutdown(wait, cancel_futures=cancel_futures)
            self.__class__._instances.pop(self.__class__, None)



class _AppMeta(ABCMeta):
    """Singleton metaclass for UniteIO."""
    _main: Optional[asyncio.Task] = None
    _instances: dict[type[UniteIO], UniteIO] = {}
    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            uio = cls._instances[cls] = super(_AppMeta, cls).__call__(*args, **kwargs)
            cls._main = uio.loop.create_task(uio(*args, *kwargs), name=cls.__name__)
            cls._main.add_done_callback()
        return cls._instances[cls]



class UniteIO(Thread, metaclass=_AppMeta):
    """UniteIO class for handling I/O operations in a unified manner.

    This class provides a unified interface for performing I/O operations
    asynchronously using asyncio and ThreadPoolExecutor. It allows for
    efficient execution of blocking I/O operations in a separate thread
    pool while keeping the main event loop responsive.
    """

    _name: ClassVar[str]

    pool: UIOPool
    loop: asyncio.AbstractEventLoop

    tasks: set[asyncio.Task]
    groups: set[asyncio.TaskGroup]

    @property
    def kls(self) -> type[UniteIO]:
        return self.__class__


    @classmethod
    def __init_subclass__(cls, name=None, **kwargs):
        cls._name = name or cls.__name__
        pass


    @classmethod
    def shutdown(cls, wait=True):
        """Shutdown all UniteIO applications."""
        for appl in cls._instances.values():
            [task.cancel() for task in cls._maintasks]
            appl.stop(wait)
        cls._instances = {}
        cls._maintasks = set()


    def taskname(self) -> str:
        """Generate a unique task name."""
        counter = 0
        while True:
            counter += 1
            yield f'{self.name}:{counter}'



    def __init__(self, *args, **kwargs):
        """Initialize the UniteIO instance."""
        self.pool = UIOPool()
        self.loop = self.pool.get_loop()
        self.done = self.loop.create_future()

        super(UniteIO, self).__init__(
            target=self.loop.run_until_complete,
            name=f'{self.kls._name}:Async',
            kwargs={"future": self.done}
        )

        self.tasks = set()
        self.start()
        self.rlock = RLock()


    @abstractmethod
    async def __call__(self, *args, **kwargs):
        """Abstract method to be implemented by subclasses with the actual logic"""
        pass


    def stop(self, wait):
        """Stop the UniteIO application."""
        ## =>> TODO :: Better Shutdown!
        [fut.cancel() for fut in  list(asyncio.all_tasks(self.loop))+[self.done]]
        self.loop.stop()


    async def asleep(self, delay, result=None):
        """Asynchronously sleep for a specified duration."""
        return await asyncio.sleep(delay, result or self)


    def sleep(self, delay, result=None):
        """Sleep for a specified duration."""
        time.sleep(delay)
        return result or self


    def wait(self, *foo, timeout=None):
        asyncio.wait(set(foo), timeout=timeout)
        return self



    @overload
    def submit(self, target: Coroutine, /,*, name: Optional[str] = None, eager: bool = False) -> asyncio.Task:...
    @overload
    def submit(self, target: Iterable[Coroutine], /,*, name: Optional[str] = None, eager: bool = False) -> asyncio.Task:...

    def submit(self, target: Callable, /, *args, name: Optional[str] = None, eager: bool = False, **kwargs) -> asyncio.Task:
        """Task submission to the UniteIO application."""

        if isinstance(target, Iterable):
            if all(map(asyncio.iscoroutine, target)):
                raise TypeError("For multiple tasks submission, allmembers must be coroutines")
            coro = self._task_group_coro(coroset=target, name=name or next(self.taskname()))
            
        elif asyncio.iscoroutine(target):
            coro = target

        elif asyncio.iscoroutinefunction(target):
            coro = target(*args, **kwargs)

        elif callable(target):
            coro = asyncio.to_thread(target, *args, **kwargs)
            
        else:
            raise TypeError("Target must be a coroutine, coroutine function or callable")

        task = self.loop.create_task(coro, name=name or next(self.taskname()), eager_start=eager)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task


    @staticmethod
    async def _task_group_coro(coroset: Iterable[Coroutine], name: str):
        """Convert an iterable of coroutines into a coroutine that waits for all of them."""
        fooret = dict.fromkeys(coroset, asyncio.Task.result)
        async with asyncio.TaskGroup() as tg:
            for co,fr in fooret.items():
                tg.create_task(co, name=f"{name}").add_done_callback(fr)
        return fooret



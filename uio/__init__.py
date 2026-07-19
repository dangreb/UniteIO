
"""Public package interface for UniteIO.

The package exports :class:`UniteIO`, its shared :class:`UIOPool`, and the
optional :class:`AdjustableThreadPoolExecutor` implementation.

Example:
    Create an application whose asynchronous entry point runs on its own
    event-loop thread::

        import asyncio
        from uio import UniteIO

        class Worker(UniteIO):
            async def __call__(self):
                await asyncio.Event().wait()

        worker = Worker()
        worker.stop()
"""

from uio.uniteio import *
from uio.adjustable_pool import *

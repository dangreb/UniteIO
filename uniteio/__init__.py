"""UniteIO: asyncio applications on dedicated threads, sharing one executor.

Example:
    Start an application on its own loop thread and stop it again::

        import asyncio
        from uniteio import App, Runtime

        class Worker(App):
            async def __call__(self):
                await asyncio.Event().wait()

        with Runtime() as rt:
            worker = rt.start(Worker())
"""

from uniteio.runtime import *

UniteIO
=======

UniteIO runs several asyncio applications in one Python process, each on its
own thread with its own event loop. The applications share the process, its
state, and one thread-pool executor for synchronous work.

A :class:`~uniteio.Runtime` owns the executor, a registry of running
applications, and their threads. An :class:`~uniteio.App` is a class with an
``async __call__``; the runtime starts it on a new thread and runs that
coroutine on the application's loop.

.. code-block:: python

   import asyncio
   from uniteio import App, Runtime

   class Clock(App):
       async def __call__(self):
           while True:
               await asyncio.sleep(1)

   with Runtime() as rt:
       clock = rt.start(Clock())

UniteIO works with and without the GIL. On a free-threaded build, Python code on
different application threads can run in parallel; with the GIL, the loops are
still independent, but their Python code takes turns.

.. note::

   UniteIO is an early alpha and requires Python 3.14.5 or newer. The API may
   still change between minor versions.

.. toctree::
   :maxdepth: 2
   :caption: Contents

   quickstart
   guide
   api

Indices
-------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`

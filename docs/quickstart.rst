Quick start
===========

Installation
------------

Install the package into a free-threaded Python 3.14.5 or newer environment::

   pip install uniteio

UniteIO warns when the interpreter's GIL is enabled. The library can still
provide separate event loops in that configuration, but parallel execution of
Python code between their threads is the capability free-threaded CPython adds.

Check whether the interpreter supports free threading and whether the GIL is
currently enabled:

.. code-block:: console

   $ python -c "import sys, sysconfig; print('free-threaded build:', bool(sysconfig.get_config_var('Py_GIL_DISABLED'))); print('GIL enabled:', sys._is_gil_enabled())"
   free-threaded build: True
   GIL enabled: False

A free-threaded build can optionally run with the GIL enabled, and importing an
incompatible C extension may enable it automatically. Python documents the
runtime controls and compatibility considerations in its
`free-threading guide <https://docs.python.org/3/howto/free-threading-python.html>`_.

One runtime, two independent loops
----------------------------------

Consider a market-data process that consumes two Binance websocket streams.
Trade bursts and ticker updates have different processing characteristics, but
the consumers need access to the same in-process market state.

Model each long-running consumer as a different :class:`uniteio.UniteIO`
subclass. The stream below is a self-contained stand-in for an async iterator
provided by a Binance websocket client:

.. code-block:: python

   import asyncio
   from threading import Lock

   from uniteio import UniteIO


   class MarketState:
       """State deliberately shared by both event-loop threads."""

       def __init__(self):
           self._lock = Lock()
           self.values = {}

       def update(self, key, value):
           with self._lock:
               self.values[key] = value


   async def demo_stream(event_type):
       """Replace this with the async iterator from a websocket client."""
       sequence = 0
       while True:
           await asyncio.sleep(0.25)
           sequence += 1
           yield {"type": event_type, "sequence": sequence}


   class TradeStream(UniteIO, prefix="TRD"):
       def __init__(self, state):
           super().__init__(state=state)

       async def __call__(self) -> None:
           async for trade in demo_stream("trade"):
               self.state.update("last_trade", trade)


   class TickerStream(UniteIO, prefix="TCK"):
       def __init__(self, state):
           super().__init__(state=state)

       async def __call__(self) -> None:
           async for ticker in demo_stream("ticker"):
               self.state.update("last_ticker", ticker)


   state = MarketState()
   trades = TradeStream(state=state)
   ticker = TickerStream(state=state)

``trades`` and ``ticker`` now run concurrently on different event loops and
different threads. On free-threaded CPython, eligible Python callbacks on those
threads may also run in parallel. They share ``state`` by reference, so its
mutable dictionary is protected by :class:`threading.Lock`.

Each concrete subclass has one active instance. Constructing ``TradeStream`` a
second time returns the same running trade application; the different
``TickerStream`` subclass receives its own application, thread, and loop.

Submitting work
---------------

Submit coroutine work to a particular application's loop from synchronous
code:

.. code-block:: python

   ready = trades.submit(
       asyncio.sleep,
       0,
       result="trade loop ready",
       name="warmup",
   )
   assert ready.result() == "trade loop ready"

The target application determines the destination loop. This makes it possible
to keep task groups, timeouts, and loop-local resources isolated by concern.

Synchronous callables submitted through ``submit`` run on the shared
:class:`uniteio.UIOPool` instead:

.. code-block:: python

   total = ticker.submit(sum, [1, 2, 3])
   assert total.result() == 6

The shared executor is useful for ordinary blocking or synchronous work. It is
separate from the dedicated threads that drive the application event loops.

What independence means
-----------------------

* A slow callback blocks its own loop thread, not the other application's loop.
* Loop-local tasks, async generators, and cancellation are owned by one app.
* Free-threaded CPython permits Python work on different loop threads to run in
  parallel; actual speedups still depend on the workload and libraries used.
* Globals and passed objects remain genuinely shared. Mutable access must be
  synchronized, and cross-loop asyncio primitives should not be shared.

Shutdown
--------

Stop one stream without affecting the other loop or shared executor::

   trades.stop(timeout=2)

   # The ticker application remains active.
   ticker.stop(timeout=2)

Or stop every registered application before shutting down the executor::

   from uniteio import UIOPool

   UIOPool().shutdown(cancel_futures=True)

Broadcasting one stream across applications
===========================================

``EmitIO`` lets one UniteIO application own a connection and broadcast its
events to consumers running on independent event loops.  Each consumer receives
its own subscription and chooses how much data to buffer when it cannot keep up.

This is useful for a price-ticker websocket that must serve consumers with
different requirements.  A database writer can preserve every subsequent tick,
while TP/SL logic can discard superseded prices and retain only the latest one.

An emitting application
-----------------------

Declare emitter defaults with the ``emitter`` class option.  The options are
validated when the class is defined, and each new application instance receives
a fresh :class:`uniteio.IterEmitter`:

.. code-block:: python

   from uniteio import EmitIO, OverflowPolicy


   class PriceFeed(
       EmitIO,
       prefix="PX",
       emitter={
           "buffer": 1024,
           "overflow": OverflowPolicy.DISCONNECT,
       },
   ):
       def __init__(self, websocket):
           super().__init__(websocket=websocket)

       async def __call__(self) -> None:
           try:
               async for ticker in self.websocket:
                   self.emit(ticker)
           finally:
               self.close_emitter()

``emit`` is synchronous and may be called from any thread.  It serializes
concurrent producers and schedules a small delivery callback on every
subscriber's own event loop.  It does not execute subscriber business logic.

Consumer-specific buffering
---------------------------

Applications can iterate over the emitting application directly.  That uses
the emitter defaults::

   async for ticker in PriceFeed():
       ...

Use :meth:`uniteio.EmitIO.with_buffer` when a consumer needs different
behavior:

.. code-block:: python

   from uniteio import OverflowPolicy, UniteIO


   class TickArchive(UniteIO, prefix="DB"):
       async def __call__(self) -> None:
           # None selects an unbounded subscription for this consumer.
           async for ticker in PriceFeed().with_buffer(None):
               await database.insert_ticker(ticker)


   class ExitStrategy(UniteIO, prefix="TPSL"):
       async def __call__(self) -> None:
           # At most one pending price: a newer tick supersedes an older one.
           async for ticker in PriceFeed().with_buffer(
               1,
               overflow=OverflowPolicy.DROP_OLDEST,
           ):
               await evaluate_take_profit_and_stop_loss(ticker)

Awaiting an asynchronous database operation suspends only the archive task; it
does not block that application's event loop.  A synchronous database client
should instead run through ``asyncio.to_thread`` or the shared executor.

Overflow policies
-----------------

``OverflowPolicy.DISCONNECT``
   Clear the slow subscription, deregister it, and raise
   :class:`uniteio.SlowConsumerError` in that consumer.  This is the default
   because it makes unexpected data loss visible.

``OverflowPolicy.DROP_OLDEST``
   Remove the oldest queued value and retain the incoming value.  A one-item
   buffer therefore represents the latest pending state, which is useful for
   ticker-driven decisions.

``OverflowPolicy.DROP_NEWEST``
   Reject the incoming value and preserve values already queued for the
   consumer.

An unbounded buffer preserves all events after registration, but a permanently
slow consumer can use unbounded memory.  Persistent event streams should pair
this choice with monitoring or an external durable queue.

Live-subscription semantics
---------------------------

An iteration receives only values emitted after it registers.  This is a live
broadcast, not a replay log.  Startup code and tests that require a known number
of consumers can coordinate synchronously with
:meth:`uniteio.IterEmitter.wait_for_subscribers`::

   feed = PriceFeed(websocket=websocket)
   archive = TickArchive()
   strategy = ExitStrategy()

   if not feed.emitter.wait_for_subscribers(2, timeout=5):
       raise RuntimeError("market-data consumers did not start")

Do not call this blocking helper from an event-loop thread.

Closure and shutdown
--------------------

:meth:`uniteio.IterEmitter.close` is permanent and idempotent.  Payloads already
scheduled for a subscriber remain ahead of its finish notification.  A
synchronous coordinator can wait until all subscriber buffers drain::

   if not feed.close_emitter(wait=True, timeout=5):
       raise RuntimeError("subscribers did not drain")

   archive.stop()
   strategy.stop()
   feed.stop()

Waiting is rejected from a subscriber's own event loop because that would
deadlock the loop required to drain its buffer.  ``EmitIO.stop()`` closes its
owned emitter without waiting; request graceful draining explicitly before
stopping applications or the shared pool.

Standalone emitters
-------------------

``IterEmitter`` does not require an ``EmitIO`` owner.  It can be instantiated
and shared like any other synchronized process-wide object:

.. code-block:: python

   from uniteio import IterEmitter, OverflowPolicy

   events = IterEmitter(
       buffer=256,
       overflow=OverflowPolicy.DISCONNECT,
   )

   events.emit({"type": "ready"})
   events.close()

The ``emitted_count``, ``dropped_count``, and ``subscriber_count`` properties
provide basic process-local instrumentation.

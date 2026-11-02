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

Declare emitter defaults with the immutable :class:`uniteio.EmitterOptions`
class option.  ``SubscriptionOptions`` validates delivery policy when it is
constructed, and each new application instance receives a fresh
:class:`uniteio.IterEmitter`:

.. code-block:: python

   from uniteio import EmitIO, EmitterOptions, OverflowPolicy, SubscriptionOptions


   class PriceFeed(
       EmitIO[dict[str, str]],
       prefix="PX",
       emitter=EmitterOptions(
           subscription=SubscriptionOptions(
               buffer=1024,
               overflow=OverflowPolicy.DISCONNECT,
           ),
       ),
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

The options object is immutable and can produce a validated partial copy for
the general subscription API::

   archive_options = PriceFeed().emitter.options.with_overrides(buffer=None)

   async for ticker in PriceFeed().subscribe(options=archive_options):
       await database.insert_ticker(ticker)

Routing selected payloads
-------------------------

By default, an emitter broadcasts every payload to every active subscription.
An emitter can instead receive a synchronous, side-effect-free router that
derives one hashable key or several keys from each payload.  A subscriber can
then declare immutable wanted and unwanted keys.  Only matching subscriptions
are scheduled on their event loops; unmatched payloads do not enter their
buffers.

The router defines the key vocabulary.  It can expose broad and composite keys
when consumers need both kinds of interest:

.. code-block:: python

   from uniteio import (
       EmitIO,
       EmitterOptions,
       OverflowPolicy,
       SubscriptionCriteria,
       SubscriptionOptions,
   )


   def ticker_route_keys(ticker: dict[str, str]):
       event = ticker["event"]
       symbol = ticker["symbol"]
       return {
           ("event", event),
           ("symbol", symbol),
           ("event", event, "symbol", symbol),
       }


   class PriceFeed(
       EmitIO[dict[str, str]],
       emitter=EmitterOptions(
           subscription=SubscriptionOptions(buffer=1024),
           router=ticker_route_keys,
       ),
   ):
       ...


   btc_ticker = SubscriptionCriteria(
       wanted=frozenset({("event", "ticker", "symbol", "BTCUSDT")}),
   )

   async for ticker in PriceFeed().with_criteria(
       btc_ticker,
       options=SubscriptionOptions(
           buffer=1,
           overflow=OverflowPolicy.DROP_OLDEST,
       ),
   ):
       await evaluate_take_profit_and_stop_loss(ticker)

``wanted=None`` is a wildcard; this is the behavior used by direct iteration
and :meth:`uniteio.EmitIO.subscribe` with no criteria.  ``wanted`` keys have
any-of semantics, while any matching ``unwanted`` key rejects the payload.  A
router can expose a composite key, as above, when an interest needs a logical
conjunction.  A subscription is scheduled at most once even if several wanted
keys match the same payload.

Criteria require an emitter router.  Arbitrary payload predicates are not part
of this API because they would require testing every subscriber for every
payload and defeat indexed routing.  Keep routers short and synchronous: they
run on the calling thread while ``emit`` serializes producer order.

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

   from uniteio import IterEmitter, OverflowPolicy, SubscriptionOptions

   events = IterEmitter(
       SubscriptionOptions(
           buffer=256,
           overflow=OverflowPolicy.DISCONNECT,
       ),
   )

   events.emit({"type": "ready"})
   events.close()

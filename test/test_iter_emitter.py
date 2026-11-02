import asyncio

import pytest

from uniteio import (
    EmitterOptions,
    EmitIO,
    IterEmitter,
    OverflowPolicy,
    SlowConsumerError,
    SubscriptionCriteria,
    SubscriptionOptions,
    UniteIO,
)


async def wait_for_subscribers(emitter, count):
    assert await asyncio.to_thread(
        emitter.wait_for_subscribers,
        count,
        2,
    )


@pytest.mark.parametrize(
    ("buffer", "error"),
    [
        (0, ValueError),
        (-1, ValueError),
        (True, TypeError),
        (1.5, TypeError),
    ],
)
def test_subscription_options_are_validated(buffer, error):
    with pytest.raises(error):
        SubscriptionOptions(buffer=buffer)

    emitter = IterEmitter()
    with pytest.raises(error):
        emitter.with_buffer(buffer)

    with pytest.raises(TypeError):
        emitter.with_buffer(1, overflow="drop-oldest")


def test_subscription_options_are_immutable_and_replaceable():
    defaults = SubscriptionOptions()
    latest = defaults.with_overrides(
        buffer=1,
        overflow=OverflowPolicy.DROP_OLDEST,
    )

    assert defaults.buffer == 1_024
    assert latest == SubscriptionOptions(
        buffer=1,
        overflow=OverflowPolicy.DROP_OLDEST,
    )
    with pytest.raises(TypeError):
        defaults.with_overrides(unexpected=True)


def test_statistics_are_not_part_of_the_emitter_api():
    emitter = IterEmitter()

    assert not hasattr(emitter, "emitted_count")
    assert not hasattr(emitter, "dropped_count")
    assert not hasattr(emitter, "subscriber_count")


def test_late_iteration_finishes_after_close():
    async def run():
        emitter = IterEmitter[int]()
        assert emitter.close()
        assert [item async for item in emitter] == []

        with pytest.raises(RuntimeError, match="after close"):
            emitter.emit(1)

    asyncio.run(run())


def test_cancelling_one_iteration_does_not_cancel_another():
    async def run():
        emitter = IterEmitter[str](buffer=None)
        first = aiter(emitter)
        second = aiter(emitter)
        cancelled = asyncio.create_task(anext(first))
        survivor = asyncio.create_task(anext(second))

        await wait_for_subscribers(emitter, 2)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled

        await wait_for_subscribers(emitter, 1)
        assert emitter.emit("ready") == 1
        assert await survivor == "ready"
        await second.aclose()

    asyncio.run(run())


def test_consumer_specific_overflow_policies():
    async def run():
        emitter = IterEmitter[int]()
        oldest = emitter.with_buffer(
            1,
            overflow=OverflowPolicy.DROP_NEWEST,
        )
        newest = emitter.with_buffer(
            1,
            overflow=OverflowPolicy.DROP_OLDEST,
        )
        disconnected = emitter.with_buffer(
            1,
            overflow=OverflowPolicy.DISCONNECT,
        )

        oldest_value = asyncio.create_task(anext(oldest))
        newest_value = asyncio.create_task(anext(newest))
        rejected = asyncio.create_task(anext(disconnected))
        await wait_for_subscribers(emitter, 3)

        for value in range(3):
            emitter.emit(value)

        assert await oldest_value == 0
        assert await newest_value == 2
        with pytest.raises(SlowConsumerError):
            await rejected

        await oldest.aclose()
        await newest.aclose()

    asyncio.run(run())


def test_routed_subscriptions_skip_unmatched_payloads_and_deduplicate():
    def route(payload):
        return {
            ("event", payload["event"]),
            ("symbol", payload["symbol"]),
            ("event", payload["event"], "symbol", payload["symbol"]),
        }

    async def run():
        emitter = IterEmitter[dict[str, str]](router=route)
        all_events = emitter.subscribe()
        bitcoin_tickers = emitter.with_criteria(
            SubscriptionCriteria(
                wanted=frozenset({("event", "ticker", "symbol", "BTCUSDT")}),
            ),
        )
        non_bitcoin_tickers = emitter.with_criteria(
            SubscriptionCriteria(
                wanted=frozenset({("event", "ticker")}),
                unwanted=frozenset({("symbol", "BTCUSDT")}),
            ),
        )
        duplicate_key_match = emitter.with_criteria(
            SubscriptionCriteria(
                wanted=frozenset({
                    ("symbol", "BTCUSDT"),
                    ("event", "ticker", "symbol", "BTCUSDT"),
                }),
            ),
        )

        all_btc = asyncio.create_task(anext(all_events))
        btc = asyncio.create_task(anext(bitcoin_tickers))
        non_btc = asyncio.create_task(anext(non_bitcoin_tickers))
        duplicate = asyncio.create_task(anext(duplicate_key_match))
        await wait_for_subscribers(emitter, 4)

        bitcoin = {"event": "ticker", "symbol": "BTCUSDT"}
        assert emitter.emit(bitcoin) == 3
        assert await all_btc == bitcoin
        assert await btc == bitcoin
        assert await duplicate == bitcoin

        all_eth = asyncio.create_task(anext(all_events))
        ethereum = {"event": "ticker", "symbol": "ETHUSDT"}
        assert emitter.emit(ethereum) == 2
        assert await all_eth == ethereum
        assert await non_btc == ethereum

        await all_events.aclose()
        await bitcoin_tickers.aclose()
        await non_bitcoin_tickers.aclose()
        await duplicate_key_match.aclose()
        assert emitter.close(wait=True, timeout=2)

    asyncio.run(run())


def test_criteria_require_a_router_and_cancellation_cleans_the_index():
    criterion = SubscriptionCriteria(wanted=frozenset({"ticker"}))
    with pytest.raises(RuntimeError, match="router"):
        IterEmitter[str]().with_criteria(criterion)

    async def run():
        emitter = IterEmitter[str](router=lambda payload: payload)
        iterator = emitter.with_criteria(criterion)
        waiting = asyncio.create_task(anext(iterator))
        await wait_for_subscribers(emitter, 1)

        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

        assert emitter.emit("ticker") == 0
        assert emitter.close()

    asyncio.run(run())


def test_close_waits_for_subscriber_buffers_to_drain():
    async def run():
        emitter = IterEmitter[int](buffer=None)
        received = []

        async def consume():
            async for value in emitter:
                received.append(value)
                await asyncio.sleep(0)

        task = asyncio.create_task(consume())
        await wait_for_subscribers(emitter, 1)

        for value in range(50):
            emitter.emit(value)

        assert await asyncio.to_thread(
            emitter.close,
            wait=True,
            timeout=2,
        )
        await task
        assert received == list(range(50))

    asyncio.run(run())


def test_emitio_broadcasts_to_distinct_application_loops():
    archived = []
    evaluated = []

    class PriceFeed(
        EmitIO[int],
        emitter=EmitterOptions(
            subscription=SubscriptionOptions(
                buffer=1_024,
                overflow=OverflowPolicy.DISCONNECT,
            ),
        ),
    ):
        async def __call__(self):
            await asyncio.Event().wait()

    class Archive(UniteIO):
        def __init__(self, source):
            super().__init__(source=source)

        async def __call__(self):
            async for value in self.source.with_buffer(None):
                archived.append(value)
                await asyncio.sleep(0.001)

    class Strategy(UniteIO):
        def __init__(self, source):
            super().__init__(source=source)

        async def __call__(self):
            async for value in self.source.with_buffer(
                1,
                overflow=OverflowPolicy.DROP_OLDEST,
            ):
                evaluated.append(value)
                await asyncio.sleep(0.01)

    feed = PriceFeed()
    archive = Archive(source=feed)
    strategy = Strategy(source=feed)
    assert feed.emitter.wait_for_subscribers(2, timeout=2)

    for value in range(20):
        assert feed.emit(value) == 2

    assert feed.close_emitter(wait=True, timeout=2)
    assert archived == list(range(20))
    assert evaluated[-1] == 19
    assert len(evaluated) < len(archived)

    archive.stop()
    strategy.stop()
    feed.stop()


def test_emitio_validates_configuration_and_recreates_emitter():
    with pytest.raises(ValueError):

        class Invalid(EmitIO, emitter={"buffer": 0}):
            async def __call__(self):
                pass

    class Source(EmitIO[str], emitter=SubscriptionOptions(buffer=None)):
        async def __call__(self):
            await asyncio.Event().wait()

    first = Source()
    first_emitter = first.emitter
    assert first_emitter.options.buffer is None
    assert first.stop(timeout=2)
    assert first_emitter.closed

    second = Source()
    assert second.emitter is not first_emitter
    assert second.stop(timeout=2)


def test_emitio_mapping_configuration_keeps_legacy_syntax_and_adds_routing():
    def route(payload: str):
        return payload.split(":", maxsplit=1)[0]

    class RoutedSource(
        EmitIO[str],
        emitter={
            "buffer": 2,
            "router": route,
        },
    ):
        async def __call__(self):
            await asyncio.Event().wait()

    source = RoutedSource()
    assert source.emitter.options.buffer == 2

    async def receive():
        ticker = source.with_criteria(
            SubscriptionCriteria(wanted=frozenset({"ticker"})),
        )
        received = asyncio.create_task(anext(ticker))
        await wait_for_subscribers(source.emitter, 1)

        assert source.emit("ticker:BTCUSDT") == 1
        assert await received == "ticker:BTCUSDT"
        await ticker.aclose()

    asyncio.run(receive())
    assert source.stop(timeout=2)

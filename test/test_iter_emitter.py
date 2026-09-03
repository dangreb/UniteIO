import asyncio

import pytest

from uniteio import (
    EmitIO,
    IterEmitter,
    OverflowPolicy,
    SlowConsumerError,
    UniteIO,
)


async def wait_for_subscribers(emitter, count):
    for _ in range(1_000):
        if emitter.subscriber_count == count:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"expected {count} subscribers, found {emitter.subscriber_count}"
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
        IterEmitter(buffer=buffer)

    emitter = IterEmitter()
    with pytest.raises(error):
        emitter.with_buffer(buffer)

    with pytest.raises(TypeError):
        emitter.with_buffer(1, overflow="drop-oldest")


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
        assert emitter.dropped_count == 5

        await oldest.aclose()
        await newest.aclose()

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

    class PriceFeed(EmitIO, emitter={
        "buffer": 1_024,
        "overflow": OverflowPolicy.DISCONNECT,
    }):
        async def __call__(self):
            await asyncio.Event().wait()

    class Archive(UniteIO):
        def __init__(self, source):
            super().__init__(source=source)

        async def __call__(self):
            async for value in self.source.where(None):
                archived.append(value)
                await asyncio.sleep(0.001)

    class Strategy(UniteIO):
        def __init__(self, source):
            super().__init__(source=source)

        async def __call__(self):
            async for value in self.source.where(
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
    assert feed.emitter.dropped_count > 0

    archive.stop()
    strategy.stop()
    feed.stop()


def test_emitio_validates_configuration_and_recreates_emitter():
    with pytest.raises(ValueError):

        class Invalid(EmitIO, emitter={"buffer": 0}):
            async def __call__(self):
                pass

    class Source(EmitIO, emitter={}):
        async def __call__(self):
            await asyncio.Event().wait()

    first = Source()
    first_emitter = first.emitter
    assert first.stop(timeout=2)
    assert first_emitter.closed

    second = Source()
    assert second.emitter is not first_emitter
    assert second.stop(timeout=2)

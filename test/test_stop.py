import asyncio
import threading
from concurrent.futures import CancelledError

import pytest

from uniteio import UniteIO, UIOPool


class RunningApplication(UniteIO):
    def __init__(self):
        self.started = threading.Event()
        self.cancelled = threading.Event()
        super().__init__()

    async def __call__(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()


class SelfStoppingApplication(UniteIO):
    def __init__(self):
        self.stop_returned = threading.Event()
        super().__init__()

    async def __call__(self):
        self.stop()
        self.stop_returned.set()
        await asyncio.sleep(60)


def test_stop_closes_only_the_application():
    app = RunningApplication()
    assert app.started.wait(2)

    pool = UIOPool()
    assert app.stop(timeout=2)

    assert not app.is_alive()
    assert app.loop.is_closed()
    assert app.cancelled.is_set()
    assert pool.submit(lambda: 42).result(timeout=2) == 42


def test_stopped_application_can_be_started_again():
    first = RunningApplication()
    assert first.started.wait(2)
    assert first.stop(timeout=2)

    second = RunningApplication()
    assert second is not first
    assert second.started.wait(2)
    assert second.stop(timeout=2)


def test_application_can_request_its_own_stop():
    app = SelfStoppingApplication()

    assert app.stop_returned.wait(2)
    app.join(2)

    assert not app.is_alive()
    assert app.loop.is_closed()
    assert app._stopped.is_set()


def test_stop_without_wait_returns_before_join_and_eventually_closes():
    app = RunningApplication()
    assert app.started.wait(2)

    stopped_synchronously = app.stop(wait=False)

    assert stopped_synchronously is False
    assert app._stopped.wait(2)
    app.join(2)
    assert not app.is_alive()
    assert app.loop.is_closed()


def test_stop_is_idempotent():
    app = RunningApplication()
    assert app.started.wait(2)

    assert app.stop(timeout=2)
    assert app.stop(timeout=2)


def test_stop_cancels_submitted_tasks():
    app = RunningApplication()
    assert app.started.wait(2)
    task_cancelled = threading.Event()

    async def pending_work():
        try:
            await asyncio.Event().wait()
        finally:
            task_cancelled.set()

    future = app.submit(pending_work(), name="pending")
    assert app.stop(timeout=2)

    assert task_cancelled.wait(2)
    with pytest.raises(CancelledError):
        future.result(2)


def test_stop_finalizes_async_generators():
    generator_started = threading.Event()
    generator_closed = threading.Event()

    class AsyncGeneratorApplication(UniteIO):
        async def values(self):
            try:
                generator_started.set()
                yield 1
                await asyncio.Event().wait()
            finally:
                generator_closed.set()

        async def __call__(self):
            generator = self.values()
            await anext(generator)
            await asyncio.Event().wait()

    app = AsyncGeneratorApplication()
    assert generator_started.wait(2)

    assert app.stop(timeout=2)
    assert generator_closed.wait(2)

import asyncio
import threading
import time

import pytest

from uio import UniteIO, UIOPool


class FirstPoolApplication(UniteIO):
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


class SecondPoolApplication(FirstPoolApplication):
    pass


def test_pool_shutdown_stops_apps_before_executor():
    pool = UIOPool()
    first = FirstPoolApplication()
    second = SecondPoolApplication()

    assert first.started.wait(2)
    assert second.started.wait(2)

    pool.shutdown()

    for app in (first, second):
        assert not app.is_alive()
        assert app.loop.is_closed()
        assert app.cancelled.is_set()

    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)


def test_shutdown_pool_singleton_can_be_recreated():
    previous = UIOPool()
    previous.shutdown()

    replacement = UIOPool()
    try:
        assert replacement is not previous
        assert replacement.submit(lambda: 42).result(timeout=2) == 42
    finally:
        replacement.shutdown()


def test_pool_is_singleton_and_keeps_initial_configuration():
    first = UIOPool(max_workers=2)
    second = UIOPool(max_workers=8)

    assert first is second
    assert second.max_workers == 2


def test_repeated_shutdown_is_safe():
    pool = UIOPool()

    pool.shutdown()
    pool.shutdown()


def test_shutdown_from_application_thread_is_rejected():
    attempted = threading.Event()
    errors = []

    class ShutdownCallingApplication(UniteIO):
        async def __call__(self):
            try:
                self.pool.shutdown()
            except BaseException as exc:
                errors.append(exc)
            finally:
                attempted.set()
            await asyncio.Event().wait()

    app = ShutdownCallingApplication()

    assert attempted.wait(2)
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "app thread" in str(errors[0])
    assert app.is_alive()


def test_shutdown_with_wait_false_still_closes_apps_first():
    app = FirstPoolApplication()
    assert app.started.wait(2)
    pool = UIOPool()

    pool.shutdown(wait=False)

    assert not app.is_alive()
    assert app.loop.is_closed()
    assert app.cancelled.is_set()
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)


def test_shutdown_forwards_cancel_futures():
    pool = UIOPool(max_workers=1)
    worker_started = threading.Event()
    release_worker = threading.Event()

    def block_worker():
        worker_started.set()
        release_worker.wait(2)

    running = pool.submit(block_worker)
    queued = pool.submit(lambda: "should not run")
    assert worker_started.wait(2)

    pool.shutdown(wait=False, cancel_futures=True)

    assert queued.cancelled()
    release_worker.set()
    running.result(2)


def test_concurrent_shutdown_call_waits_for_owner():
    pool = UIOPool(max_workers=1)
    worker_started = threading.Event()
    release_worker = threading.Event()
    shutdown_errors = []

    def block_worker():
        worker_started.set()
        release_worker.wait(2)

    running = pool.submit(block_worker)
    assert worker_started.wait(2)

    def shut_down():
        try:
            pool.shutdown()
        except BaseException as exc:
            shutdown_errors.append(exc)

    owner = threading.Thread(target=shut_down)
    owner.start()
    deadline = time.monotonic() + 2
    while not pool._uio_shutdown_started and time.monotonic() < deadline:
        time.sleep(0.001)

    waiter = threading.Thread(target=shut_down)
    waiter.start()
    assert waiter.is_alive()

    release_worker.set()
    owner.join(2)
    waiter.join(2)
    running.result(2)

    assert not owner.is_alive()
    assert not waiter.is_alive()
    assert not shutdown_errors

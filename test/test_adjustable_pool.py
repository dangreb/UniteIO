import threading

import pytest

from uniteio.adjustable_pool import AdjustableThreadPoolExecutor


def test_max_workers_can_be_increased():
    pool = AdjustableThreadPoolExecutor(max_workers=1)
    try:
        assert pool.max_workers == 1
        assert pool.set_max_workers(3) == (1, 3)
        assert pool.max_workers == 3
        assert pool.submit(lambda: 42).result(2) == 42
    finally:
        pool.shutdown()


@pytest.mark.parametrize(
    ("value", "exception"),
    [
        (True, TypeError),
        (1.5, TypeError),
        ("2", TypeError),
        (0, ValueError),
        (-1, ValueError),
    ],
)
def test_invalid_max_workers_are_rejected(value, exception):
    pool = AdjustableThreadPoolExecutor(max_workers=1)
    try:
        with pytest.raises(exception):
            pool.set_max_workers(value)
    finally:
        pool.shutdown()


def test_cannot_lower_limit_below_existing_worker_count():
    pool = AdjustableThreadPoolExecutor(max_workers=2)
    release = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()

    def block(started):
        started.set()
        release.wait(2)

    first = pool.submit(block, first_started)
    second = pool.submit(block, second_started)
    try:
        assert first_started.wait(2)
        assert second_started.wait(2)
        with pytest.raises(ValueError, match="worker threads already exist"):
            pool.set_max_workers(1)
    finally:
        release.set()
        first.result(2)
        second.result(2)
        pool.shutdown()


def test_cannot_resize_after_shutdown():
    pool = AdjustableThreadPoolExecutor(max_workers=1)
    pool.shutdown()

    with pytest.raises(RuntimeError, match="after shutdown"):
        pool.set_max_workers(2)

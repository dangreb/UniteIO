

from __future__ import annotations

import operator
from concurrent.futures import ThreadPoolExecutor


__all__ = ["AdjustableThreadPoolExecutor"]


class AdjustableThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor with a mutable future thread-creation ceiling.

    This relies on CPython implementation details. It does not retire
    existing threads.
    """

    @property
    def max_workers(self) -> int:
        with self._shutdown_lock:
            return self._max_workers

    def set_max_workers(self, value: int) -> tuple[int, int]:
        if isinstance(value, bool):
            raise TypeError("max_workers must be an integer, not bool")

        try:
            value = operator.index(value)
        except TypeError as exc:
            raise TypeError("max_workers must be an integer") from exc

        if value <= 0:
            raise ValueError("max_workers must be greater than zero")

        # submit() and shutdown() use this same lock.
        with self._shutdown_lock:
            if self._shutdown:
                raise RuntimeError(
                    "cannot resize a ThreadPoolExecutor after shutdown"
                )

            if self._broken:
                raise RuntimeError(
                    f"cannot resize a broken ThreadPoolExecutor: {self._broken}"
                )

            existing_threads = len(self._threads)

            # Preserve the stated invariant that the pool has at most
            # max_workers threads.
            if value < existing_threads:
                raise ValueError(
                    f"cannot lower max_workers to {value}; "
                    f"{existing_threads} worker threads already exist"
                )

            previous = self._max_workers
            self._max_workers = value
            return previous, value
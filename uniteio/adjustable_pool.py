

"""Thread-pool executor whose worker ceiling can be increased at runtime.

The implementation intentionally relies on CPython's private
``ThreadPoolExecutor`` state. It changes the ceiling used when deciding
whether new workers may be created; it never terminates existing workers.
"""

from __future__ import annotations

import operator
from concurrent.futures import ThreadPoolExecutor


__all__ = ["AdjustableThreadPoolExecutor"]


class AdjustableThreadPoolExecutor(ThreadPoolExecutor):
    """A ``ThreadPoolExecutor`` with a mutable worker-creation ceiling.

    Lowering the ceiling is allowed only when the requested value is not
    below the number of worker threads that already exist. Existing workers
    are never retired.

    Args:
        max_workers: Initial maximum number of worker threads. All constructor
            arguments are inherited from :class:`ThreadPoolExecutor`.

    Example:
        Increase a running executor from two workers to four::

            from uniteio import AdjustableThreadPoolExecutor

            with AdjustableThreadPoolExecutor(max_workers=2) as pool:
                old, new = pool.set_max_workers(4)
                assert (old, new) == (2, 4)
    """

    @property
    def max_workers(self) -> int:
        """Return the current worker-creation ceiling.

        Returns:
            The maximum number of threads the executor may create.
        """
        with self._shutdown_lock:
            return self._max_workers

    def set_max_workers(self, value: int) -> tuple[int, int]:
        """Change the ceiling used for future worker creation.

        This method does not create workers immediately and does not retire
        workers that already exist.

        Args:
            value: New positive integer worker ceiling. Objects implementing
                ``__index__`` are accepted; booleans are rejected explicitly.

        Returns:
            A ``(previous, current)`` tuple containing the old and new limits.

        Raises:
            TypeError: If ``value`` is not an integer or is a boolean.
            ValueError: If ``value`` is not positive or is smaller than the
                number of workers already created.
            RuntimeError: If the executor has shut down or is broken.

        Example:
            Resize an executor before submitting additional work::

                pool = AdjustableThreadPoolExecutor(max_workers=1)
                try:
                    pool.set_max_workers(3)
                    result = pool.submit(pow, 2, 8).result()
                finally:
                    pool.shutdown()
        """
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

"""Thread-safe asynchronous broadcasts across independent event loops.

``IterEmitter`` is an asynchronous iterable.  Each iteration creates an
independent subscription owned by the event loop that starts it.  Producers may
call :meth:`IterEmitter.emit` from any thread; delivery is transferred to each
subscriber with :meth:`asyncio.loop.call_soon_threadsafe`.

``EmitIO`` combines an emitter with a :class:`~uniteio.UniteIO` application
without changing the base ``UniteIO`` lifecycle.  It is useful when one
application owns a websocket connection and broadcasts its events to
applications running on other loops.
"""

from __future__ import annotations

import asyncio

from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
from threading import Condition, Lock, RLock
from typing import Any, ClassVar, Generic, TypeVar

from uniteio.uniteio import UniteIO


__all__ = [
    "EmitIO",
    "IterEmitter",
    "OverflowPolicy",
    "SlowConsumerError",
]


T = TypeVar("T")
_EMITTER_UNSET = object()


class OverflowPolicy(Enum):
    """Action taken when a bounded subscriber buffer is full."""

    DROP_OLDEST = auto()
    """Discard the oldest queued value and retain the incoming value."""

    DROP_NEWEST = auto()
    """Discard the incoming value and retain the values already queued."""

    DISCONNECT = auto()
    """Remove the slow subscriber and raise :class:`SlowConsumerError`."""


class SlowConsumerError(RuntimeError):
    """Raised in a subscriber disconnected because its buffer filled."""


@dataclass(eq=False, slots=True)
class _Subscriber(Generic[T]):
    """Subscription state mutated only by its owning event-loop thread."""

    emitter: IterEmitter[T]
    loop: asyncio.AbstractEventLoop
    buffer: int | None
    overflow: OverflowPolicy
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    items: deque[T] = field(default_factory=deque)
    active: bool = True
    closed: bool = False
    error: BaseException | None = None

    def deliver(self, payload: T) -> None:
        """Deliver one payload from the subscriber's event-loop thread."""
        if not self.active or self.closed:
            return

        if self.buffer is not None and len(self.items) >= self.buffer:
            self.emitter._record_drop()

            if self.overflow is OverflowPolicy.DROP_OLDEST:
                self.items.popleft()
            elif self.overflow is OverflowPolicy.DROP_NEWEST:
                return
            else:
                self.items.clear()
                self.error = SlowConsumerError(
                    f"subscriber buffer reached its {self.buffer}-item limit"
                )
                self.active = False
                self.emitter._discard(self)
                self.ready.set()
                return

        self.items.append(payload)
        self.ready.set()

    def finish(self) -> None:
        """Finish after payload callbacks already scheduled on this loop."""
        if not self.active:
            return
        self.closed = True
        self.ready.set()


class IterEmitter(Generic[T]):
    """Broadcast values to async iterations running on independent loops.

    Args:
        buffer: Default maximum queued values per subscriber. ``None`` selects
            an unbounded buffer.
        overflow: Default policy applied when a subscriber buffer is full.

    Iterations receive values emitted only after they register.  Concurrent
    calls to :meth:`emit` are serialized so every subscriber observes the same
    producer order.  Individual consumers can override the default buffering
    behavior through :meth:`with_buffer`.
    """

    def __init__(
        self,
        *,
        buffer: int | None = 1_024,
        overflow: OverflowPolicy = OverflowPolicy.DISCONNECT,
    ) -> None:
        self._validate_subscription_options(buffer, overflow)

        self._buffer = buffer
        self._overflow = overflow
        self._lock = RLock()
        self._stats_lock = Lock()
        self._changed = Condition(self._lock)
        self._subscribers: set[_Subscriber[T]] = set()
        self._closed = False
        self._emitted = 0
        self._dropped = 0

    @property
    def closed(self) -> bool:
        """Whether this emitter has permanently closed."""
        with self._lock:
            return self._closed

    @property
    def emitted_count(self) -> int:
        """Number of calls to :meth:`emit` accepted before closure."""
        with self._lock:
            return self._emitted

    @property
    def dropped_count(self) -> int:
        """Total values dropped or rejected across all subscribers."""
        with self._stats_lock:
            return self._dropped

    @property
    def subscriber_count(self) -> int:
        """Number of currently registered iterations."""
        with self._lock:
            return len(self._subscribers)

    def emit(self, payload: T) -> int:
        """Schedule ``payload`` for every subscriber.

        Args:
            payload: Value delivered to every iteration registered when this
                method linearizes under the emitter lock.

        Returns:
            Number of subscriber loops on which delivery was scheduled.

        Raises:
            RuntimeError: If the emitter has already closed.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot emit after close")

            self._emitted += 1
            delivered = 0

            for subscriber in tuple(self._subscribers):
                try:
                    subscriber.loop.call_soon_threadsafe(
                        subscriber.deliver,
                        payload,
                    )
                except RuntimeError:
                    # The owner loop closed without allowing the iterator's
                    # finally block to deregister.
                    self._subscribers.discard(subscriber)
                    self._changed.notify_all()
                else:
                    delivered += 1

            return delivered

    def close(
        self,
        *,
        wait: bool = False,
        timeout: float | None = None,
    ) -> bool:
        """Permanently close this emitter, optionally waiting for consumers.

        Payloads scheduled before closure remain ahead of the finish callback on
        each subscriber loop.  Active subscribers therefore drain their local
        buffers before their iterations finish.  Later iterations finish
        immediately.

        Args:
            wait: Whether to wait for every active subscription to deregister.
            timeout: Maximum seconds to wait, or ``None`` for no timeout.

        Returns:
            Whether no subscribers remain when this method returns.

        Raises:
            RuntimeError: If waiting would block a subscriber's own loop.
        """
        running_loop: asyncio.AbstractEventLoop | None = None
        if wait:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

        with self._changed:
            if wait and any(
                subscriber.loop is running_loop
                for subscriber in self._subscribers
            ):
                raise RuntimeError(
                    "close(wait=True) cannot run on a subscriber's event loop"
                )

            if not self._closed:
                self._closed = True

                for subscriber in tuple(self._subscribers):
                    try:
                        subscriber.loop.call_soon_threadsafe(subscriber.finish)
                    except RuntimeError:
                        self._subscribers.discard(subscriber)

                self._changed.notify_all()

            if wait:
                return self._changed.wait_for(
                    lambda: not self._subscribers,
                    timeout,
                )
            return not self._subscribers

    def wait_for_subscribers(
        self,
        count: int,
        timeout: float | None = None,
    ) -> bool:
        """Block until at least ``count`` subscriptions have registered.

        This synchronous coordination helper is intended for startup code and
        tests.  It must not be called from an event-loop thread.

        Args:
            count: Minimum number of active subscriptions.
            timeout: Maximum seconds to wait, or ``None`` for no timeout.

        Returns:
            Whether the requested count was reached before closure or timeout.
        """
        if count < 0:
            raise ValueError("count must not be negative")

        with self._changed:
            return self._changed.wait_for(
                lambda: len(self._subscribers) >= count or self._closed,
                timeout,
            ) and len(self._subscribers) >= count

    def __aiter__(self) -> AsyncIterator[T]:
        """Create a subscription using this emitter's default policy."""
        return self._iterate(self._buffer, self._overflow)

    def with_buffer(
        self,
        buffer: int | None,
        *,
        overflow: OverflowPolicy | None = None,
    ) -> AsyncIterator[T]:
        """Create a subscription with its own buffering policy.

        Args:
            buffer: Maximum queued values for this subscriber. ``None`` selects
                an unbounded buffer.
            overflow: Subscriber-specific overflow policy. ``None`` inherits
                the emitter's default.

        Returns:
            An async iterator ready to register on first iteration.
        """
        selected_overflow = self._overflow if overflow is None else overflow
        self._validate_subscription_options(buffer, selected_overflow)
        return self._iterate(buffer, selected_overflow)

    async def _iterate(
        self,
        buffer: int | None,
        overflow: OverflowPolicy,
    ) -> AsyncIterator[T]:
        subscriber = _Subscriber(
            emitter=self,
            loop=asyncio.get_running_loop(),
            buffer=buffer,
            overflow=overflow,
        )

        if not self._register(subscriber):
            return

        try:
            while True:
                if subscriber.error is not None:
                    raise subscriber.error

                if subscriber.items:
                    yield subscriber.items.popleft()
                    continue

                if subscriber.closed:
                    return

                # No callback can run between these checks and clear(), because
                # all three operations execute on the subscriber's loop thread.
                subscriber.ready.clear()
                await subscriber.ready.wait()
        finally:
            subscriber.active = False
            self._discard(subscriber)

    def _register(self, subscriber: _Subscriber[T]) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._subscribers.add(subscriber)
            self._changed.notify_all()
            return True

    def _discard(self, subscriber: _Subscriber[T]) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)
                self._changed.notify_all()

    def _record_drop(self) -> None:
        with self._stats_lock:
            self._dropped += 1

    @staticmethod
    def _validate_subscription_options(
        buffer: int | None,
        overflow: OverflowPolicy,
    ) -> None:
        if buffer is not None:
            if not isinstance(buffer, int) or isinstance(buffer, bool):
                raise TypeError("buffer must be an int or None")
            if buffer < 1:
                raise ValueError("buffer must be positive or None")
        if not isinstance(overflow, OverflowPolicy):
            raise TypeError("overflow must be an OverflowPolicy")


class EmitIO(UniteIO):
    """A UniteIO application with an optional owned :class:`IterEmitter`.

    Supply emitter defaults at class definition time::

        class PriceFeed(EmitIO, emitter={
            "buffer": 1024,
            "overflow": OverflowPolicy.DISCONNECT,
        }):
            ...

    The configuration is validated when the subclass is defined.  A fresh
    emitter is created whenever a fresh singleton application is constructed.
    Subclasses inherit their parent's configuration unless they explicitly pass
    ``emitter=None``.
    """

    _emitter_options: ClassVar[dict[str, Any] | None] = None

    @classmethod
    def __init_subclass__(
        cls,
        *,
        emitter: Mapping[str, Any] | None | object = _EMITTER_UNSET,
        **kwargs: Any,
    ) -> None:
        """Configure an application-owned emitter and UniteIO class options."""
        super().__init_subclass__(**kwargs)

        if emitter is _EMITTER_UNSET:
            inherited = getattr(cls, "_emitter_options", None)
            cls._emitter_options = None if inherited is None else dict(inherited)
            return

        if emitter is None:
            cls._emitter_options = None
            return

        if not isinstance(emitter, Mapping):
            raise TypeError("emitter must be a mapping or None")

        unknown = set(emitter) - {"buffer", "overflow"}
        if unknown:
            names = ", ".join(sorted(map(str, unknown)))
            raise TypeError(f"unknown emitter option(s): {names}")

        options = {
            "buffer": emitter.get("buffer", 1_024),
            "overflow": emitter.get("overflow", OverflowPolicy.DISCONNECT),
        }
        IterEmitter._validate_subscription_options(
            options["buffer"],
            options["overflow"],
        )
        cls._emitter_options = options

    def __init__(self, **kwargs: Any) -> None:
        """Create the configured emitter before initializing the app thread."""
        options = self.__class__._emitter_options
        self._iter_emitter = (
            None if options is None else IterEmitter(**options)
        )
        super().__init__(**kwargs)

    @property
    def emitter(self) -> IterEmitter[Any]:
        """Return this application's configured emitter.

        Raises:
            RuntimeError: If the application class has no emitter configuration.
        """
        if self._iter_emitter is None:
            raise RuntimeError(
                f"{self.__class__.__name__} has no configured emitter"
            )
        return self._iter_emitter

    def __aiter__(self) -> AsyncIterator[Any]:
        """Iterate using this application's default subscriber policy."""
        return aiter(self.emitter)

    def with_buffer(
        self,
        buffer: int | None,
        *,
        overflow: OverflowPolicy | None = None,
    ) -> AsyncIterator[Any]:
        """Create an application subscription with an overridden policy."""
        return self.emitter.with_buffer(buffer, overflow=overflow)

    def emit(self, payload: Any) -> int:
        """Broadcast through this application's configured emitter."""
        return self.emitter.emit(payload)

    def close_emitter(
        self,
        *,
        wait: bool = False,
        timeout: float | None = None,
    ) -> bool:
        """Close and optionally drain this application's emitter."""
        return self.emitter.close(wait=wait, timeout=timeout)

    def stop(
        self,
        wait: bool = True,
        timeout: float | None = None,
        *,
        close_emitter: bool = True,
    ) -> bool:
        """Optionally close the owned emitter before stopping the app loop.

        Emitter closure does not wait for subscribers here.  Call
        :meth:`close_emitter` explicitly before ``stop`` when graceful draining
        is required.
        """
        if close_emitter and self._iter_emitter is not None:
            self._iter_emitter.close()
        return super().stop(wait=wait, timeout=timeout)

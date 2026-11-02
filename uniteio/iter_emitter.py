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
from collections.abc import AsyncIterator, Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from threading import Condition, RLock
from typing import Any, ClassVar, Generic, Self, TypeVar, cast

from uniteio.uniteio import UniteIO




__all__ = [
    "EmitterOptions",
    "EmitIO",
    "IterEmitter",
    "OverflowPolicy",
    "SlowConsumerError",
    "SubscriptionCriteria",
    "SubscriptionOptions",
]


T = TypeVar("T")
K = TypeVar("K", bound=Hashable)
_EMITTER_UNSET = object()
_OPTION_UNSET = object()

RouteExtractor = Callable[[T], Hashable | Iterable[Hashable]]


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


@dataclass(frozen=True, slots=True)
class SubscriptionOptions:
    """Delivery policy assigned to one asynchronous subscription.

    ``None`` for :attr:`buffer` selects an unbounded queue.  Instances are
    immutable so an emitter can safely share its defaults across threads.
    """

    buffer: int | None = 1_024
    overflow: OverflowPolicy = OverflowPolicy.DISCONNECT

    def __post_init__(self) -> None:
        if self.buffer is not None:
            if not isinstance(self.buffer, int) or isinstance(self.buffer, bool):
                raise TypeError("buffer must be an int or None")
            if self.buffer < 1:
                raise ValueError("buffer must be positive or None")
        if not isinstance(self.overflow, OverflowPolicy):
            raise TypeError("overflow must be an OverflowPolicy")

    def with_overrides(self, **changes: object) -> Self:
        """Return a validated copy with the supplied fields replaced."""
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class SubscriptionCriteria(Generic[K]):
    """Keys a subscription wants and does not want from a routed emitter.

    ``wanted=None`` selects every route key, while an empty ``wanted`` set
    selects none.  A payload is selected when it shares any wanted key and no
    unwanted key.  Routers can expose composite keys for conjunctions.
    """

    wanted: frozenset[K] | None = None
    unwanted: frozenset[K] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        self._validate_keys("wanted", self.wanted)
        self._validate_keys("unwanted", self.unwanted)

    def matches(self, route_keys: frozenset[Hashable]) -> bool:
        """Return whether a payload with ``route_keys`` belongs here."""
        return (
            (self.wanted is None or bool(self.wanted.intersection(route_keys)))
            and not self.unwanted.intersection(route_keys)
        )

    @staticmethod
    def _validate_keys(
        name: str,
        keys: frozenset[Hashable] | None,
    ) -> None:
        if keys is not None and not isinstance(keys, frozenset):
            raise TypeError(f"{name} must be a frozenset or None")
        if keys is not None and not all(isinstance(key, Hashable) for key in keys):
            raise TypeError(f"{name} keys must be hashable")


@dataclass(frozen=True, slots=True)
class EmitterOptions(Generic[T]):
    """Immutable configuration for an emitter and its default subscription."""

    subscription: SubscriptionOptions = field(
        default_factory=SubscriptionOptions,
    )
    router: RouteExtractor[T] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.subscription, SubscriptionOptions):
            raise TypeError("subscription must be a SubscriptionOptions")
        if self.router is not None and not callable(self.router):
            raise TypeError("router must be callable or None")


@dataclass(eq=False, slots=True)
class _Subscriber(Generic[T]):
    """Subscription state mutated only by its owning event-loop thread."""

    emitter: IterEmitter[T]
    loop: asyncio.AbstractEventLoop
    options: SubscriptionOptions
    criteria: SubscriptionCriteria[Hashable] | None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    items: deque[T] = field(default_factory=deque)
    active: bool = True
    closed: bool = False
    error: BaseException | None = None

    def deliver(self, payload: T) -> None:
        """Deliver one payload from the subscriber's event-loop thread."""
        if not self.active or self.closed:
            return

        if (
            self.options.buffer is not None
            and len(self.items) >= self.options.buffer
        ):
            if self.options.overflow is OverflowPolicy.DROP_OLDEST:
                self.items.popleft()
            elif self.options.overflow is OverflowPolicy.DROP_NEWEST:
                return
            else:
                self.items.clear()
                self.error = SlowConsumerError(
                    "subscriber buffer reached its "
                    f"{self.options.buffer}-item limit"
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
    """Broadcast or route values to async iterations on independent loops.

    Args:
        options: Default delivery policy for new subscriptions.
        buffer: Compatibility shortcut for ``options.buffer``.
        overflow: Compatibility shortcut for ``options.overflow``.
        router: Pure callable that returns one key or an iterable of keys for
            each payload.  Supplying it enables :class:`SubscriptionCriteria`.

    Iterations receive values emitted only after they register.  Concurrent
    calls to :meth:`emit` are serialized so every subscriber observes the same
    producer order.  Individual consumers can override the default buffering
    behavior through :meth:`subscribe` or :meth:`with_buffer`.
    """

    def __init__(
        self,
        options: SubscriptionOptions | None = None,
        *,
        buffer: int | None | object = _OPTION_UNSET,
        overflow: OverflowPolicy | object = _OPTION_UNSET,
        router: RouteExtractor[T] | None = None,
    ) -> None:
        if options is not None and (
            buffer is not _OPTION_UNSET or overflow is not _OPTION_UNSET
        ):
            raise TypeError(
                "options cannot be combined with buffer or overflow"
            )
        if options is not None and not isinstance(options, SubscriptionOptions):
            raise TypeError("options must be a SubscriptionOptions or None")
        if router is not None and not callable(router):
            raise TypeError("router must be callable or None")

        if options is None:
            changes: dict[str, object] = {}
            if buffer is not _OPTION_UNSET:
                changes["buffer"] = buffer
            if overflow is not _OPTION_UNSET:
                changes["overflow"] = overflow
            options = SubscriptionOptions().with_overrides(**changes)

        self._options = options
        self._router = router
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._subscribers: set[_Subscriber[T]] = set()
        self._subscribers_by_key: dict[Hashable, set[_Subscriber[T]]] = {}
        self._wildcard_subscribers: set[_Subscriber[T]] = set()
        self._criteria_subscribers: set[_Subscriber[T]] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether this emitter has permanently closed."""
        with self._lock:
            return self._closed

    @property
    def options(self) -> SubscriptionOptions:
        """Return the immutable default delivery policy."""
        return self._options

    def emit(self, payload: T) -> int:
        """Schedule ``payload`` for every matching subscriber.

        Args:
            payload: Value routed to iterations registered when this method
                linearizes under the emitter lock.

        Returns:
            Number of matching subscriber loops on which delivery was
            scheduled.

        Raises:
            RuntimeError: If the emitter has already closed.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot emit after close")

            delivered = 0

            for subscriber in self._matching_subscribers(payload):
                try:
                    subscriber.loop.call_soon_threadsafe(
                        subscriber.deliver,
                        payload,
                    )
                except RuntimeError:
                    # The owner loop closed without allowing the iterator's
                    # finally block to deregister.
                    self._discard_locked(subscriber)
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
                        self._discard_locked(subscriber)

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
        return self.subscribe()

    def subscribe(
        self,
        *,
        options: SubscriptionOptions | None = None,
        criteria: SubscriptionCriteria[Hashable] | None = None,
    ) -> AsyncIterator[T]:
        """Create a subscription with optional delivery and routing policies.

        Args:
            options: Delivery policy for this subscriber. ``None`` inherits
                the emitter's immutable defaults.
            criteria: Keys this subscriber wants or rejects.  Criteria require
                a router configured on this emitter.

        Returns:
            An async iterator ready to register on first iteration.
        """
        if options is not None and not isinstance(options, SubscriptionOptions):
            raise TypeError("options must be a SubscriptionOptions or None")
        if criteria is not None and not isinstance(
            criteria,
            SubscriptionCriteria,
        ):
            raise TypeError("criteria must be a SubscriptionCriteria or None")
        if criteria is not None and self._router is None:
            raise RuntimeError(
                "subscription criteria require an emitter configured with a router"
            )
        return self._iterate(options or self._options, criteria)

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
        selected_overflow = self._options.overflow if overflow is None else overflow
        options = self._options.with_overrides(
            buffer=buffer,
            overflow=selected_overflow,
        )
        return self.subscribe(options=options)

    def with_criteria(
        self,
        criteria: SubscriptionCriteria[Hashable],
        *,
        options: SubscriptionOptions | None = None,
    ) -> AsyncIterator[T]:
        """Create a routed subscription with optional delivery overrides."""
        return self.subscribe(options=options, criteria=criteria)

    async def _iterate(
        self,
        options: SubscriptionOptions,
        criteria: SubscriptionCriteria[Hashable] | None,
    ) -> AsyncIterator[T]:
        subscriber = _Subscriber(
            emitter=self,
            loop=asyncio.get_running_loop(),
            options=options,
            criteria=criteria,
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
            if subscriber.criteria is None or subscriber.criteria.wanted is None:
                self._wildcard_subscribers.add(subscriber)
            else:
                for key in subscriber.criteria.wanted:
                    self._subscribers_by_key.setdefault(key, set()).add(subscriber)
            if subscriber.criteria is not None:
                self._criteria_subscribers.add(subscriber)
            self._changed.notify_all()
            return True

    def _discard(self, subscriber: _Subscriber[T]) -> None:
        with self._lock:
            self._discard_locked(subscriber)

    def _discard_locked(self, subscriber: _Subscriber[T]) -> None:
        """Remove a subscriber from every index while holding ``_lock``."""
        if subscriber not in self._subscribers:
            return

        self._subscribers.remove(subscriber)
        self._wildcard_subscribers.discard(subscriber)
        self._criteria_subscribers.discard(subscriber)
        if subscriber.criteria is not None and subscriber.criteria.wanted is not None:
            for key in subscriber.criteria.wanted:
                indexed = self._subscribers_by_key[key]
                indexed.discard(subscriber)
                if not indexed:
                    del self._subscribers_by_key[key]
        self._changed.notify_all()

    def _matching_subscribers(self, payload: T) -> tuple[_Subscriber[T], ...]:
        """Return the matching registry snapshot while holding ``_lock``."""
        if self._router is None or not self._criteria_subscribers:
            return tuple(self._subscribers)

        route_keys = self._route_keys(payload)
        candidates = set(self._wildcard_subscribers)
        for key in route_keys:
            candidates.update(self._subscribers_by_key.get(key, ()))
        return tuple(
            subscriber
            for subscriber in candidates
            if subscriber.criteria is None
            or subscriber.criteria.matches(route_keys)
        )

    def _route_keys(self, payload: T) -> frozenset[Hashable]:
        """Extract and validate route keys once for an emitted payload."""
        router = self._router
        assert router is not None
        routed = router(payload)
        if isinstance(routed, Hashable):
            return frozenset((routed,))
        try:
            return frozenset(routed)
        except TypeError as error:
            raise TypeError(
                "router must return a hashable key or an iterable of hashable keys"
            ) from error


class EmitIO(UniteIO, Generic[T]):
    """A UniteIO application with an optional owned :class:`IterEmitter`.

    Supply emitter defaults at class definition time::

        class PriceFeed(
            EmitIO[Ticker],
            emitter=EmitterOptions(
                subscription=SubscriptionOptions(buffer=1024),
                router=ticker_route_keys,
            ),
        ):
            ...

    The configuration is validated when the subclass is defined.  A fresh
    emitter is created whenever a fresh singleton application is constructed.
    Subclasses inherit their parent's configuration unless they explicitly pass
    ``emitter=None``.
    """

    _emitter_options: ClassVar[EmitterOptions[Any] | None] = None

    @classmethod
    def __init_subclass__(
        cls,
        *,
        emitter: (
            EmitterOptions[Any]
            | SubscriptionOptions
            | Mapping[str, object]
            | None
            | object
        ) = _EMITTER_UNSET,
        **kwargs: Any,
    ) -> None:
        """Configure an application-owned emitter and UniteIO class options."""
        super().__init_subclass__(**kwargs)

        if emitter is _EMITTER_UNSET:
            cls._emitter_options = getattr(cls, "_emitter_options", None) or EmitterOptions()
            return

        if emitter is None:
            cls._emitter_options = None
            return

        cls._emitter_options = cls._coerce_emitter_options(emitter)

    @staticmethod
    def _coerce_emitter_options(
        emitter: EmitterOptions[Any] | SubscriptionOptions | Mapping[str, object] | object,
    ) -> EmitterOptions[Any]:
        """Normalize the class option and retain mapping compatibility."""
        if isinstance(emitter, EmitterOptions):
            return emitter
        if isinstance(emitter, SubscriptionOptions):
            return EmitterOptions(subscription=emitter)
        if not isinstance(emitter, Mapping):
            raise TypeError(
                "emitter must be an EmitterOptions, SubscriptionOptions, "
                "mapping, or None"
            )

        values = dict(emitter)
        router = values.pop("router", None)
        try:
            subscription = SubscriptionOptions(**values)
        except TypeError as error:
            raise TypeError(f"invalid emitter option(s): {error}") from error
        return EmitterOptions(subscription=subscription, router=router)

    def __init__(self, **kwargs: Any) -> None:
        """Create the configured emitter before initializing the app thread."""
        options = self.__class__._emitter_options
        self._iter_emitter = (
            None
            if options is None
            else IterEmitter(
                options.subscription,
                router=cast(RouteExtractor[T] | None, options.router),
            )
        )
        super().__init__(**kwargs)

    @property
    def emitter(self) -> IterEmitter[T]:
        """Return this application's configured emitter.

        Raises:
            RuntimeError: If the application class has no emitter configuration.
        """
        if self._iter_emitter is None:
            raise RuntimeError(
                f"{self.__class__.__name__} has no configured emitter"
            )
        return self._iter_emitter

    def __aiter__(self) -> AsyncIterator[T]:
        """Iterate using this application's default subscriber policy."""
        return aiter(self.emitter)

    def subscribe(
        self,
        *,
        options: SubscriptionOptions | None = None,
        criteria: SubscriptionCriteria[Hashable] | None = None,
    ) -> AsyncIterator[T]:
        """Create an optional routed subscription to this application's emitter."""
        return self.emitter.subscribe(options=options, criteria=criteria)

    def with_buffer(
        self,
        buffer: int | None,
        *,
        overflow: OverflowPolicy | None = None,
    ) -> AsyncIterator[T]:
        """Create an application subscription with an overridden policy."""
        return self.emitter.with_buffer(buffer, overflow=overflow)

    def with_criteria(
        self,
        criteria: SubscriptionCriteria[Hashable],
        *,
        options: SubscriptionOptions | None = None,
    ) -> AsyncIterator[T]:
        """Create a routed application subscription."""
        return self.emitter.with_criteria(criteria, options=options)

    def emit(self, payload: T) -> int:
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

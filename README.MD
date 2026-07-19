# UniteIO

[![Documentation](https://readthedocs.org/projects/uniteio/badge/?version=latest)](https://uniteio.readthedocs.io/en/latest/)
[![CI](https://github.com/dangreb/UniteIO/actions/workflows/ci.yml/badge.svg)](https://github.com/dangreb/UniteIO/actions/workflows/ci.yml)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/dangreb/UniteIO/blob/master/LICENSE)

UniteIO runs multiple independent asyncio applications in one Python process.
Each application class is a singleton with its own dedicated thread and event
loop, while all applications can still share process-wide state and one
thread-pool executor for synchronous work.

This model is especially useful on free-threaded CPython. With the GIL
disabled, Python callbacks belonging to different event-loop threads can make
progress in parallel: a busy trade-stream consumer does not have to share an
event-loop turn with a price-ticker consumer. You keep the convenience of one
runtime and shared state without putting every asynchronous workload on one
loop.

> **Status:** UniteIO is an early alpha designed for CPython free-threaded
> environments. It requires Python 3.14.5 or newer and emits a warning when the
> interpreter's GIL is enabled.

Confirm both the build and its current runtime mode with:

```console
python -c "import sys, sysconfig; print('free-threaded build:', bool(sysconfig.get_config_var('Py_GIL_DISABLED'))); print('GIL enabled:', sys._is_gil_enabled())"
```

A free-threaded build can still run with the GIL enabled, including when an
incompatible C extension enables it. See Python's
[free-threading guide](https://docs.python.org/3/howto/free-threading-python.html).

## Installation

```console
pip install uniteio
```

## Quick start

This example mirrors an application with two websocket subscriptions. Replace
`demo_stream` with the async iterator exposed by your Binance client: the trade
and ticker consumers will continue to run on separate event loops.

```python
import asyncio
from threading import Lock

from uniteio import UniteIO


class MarketState:
    def __init__(self):
        self._lock = Lock()
        self.values = {}

    def update(self, key, value):
        # Both loop threads share this object.
        with self._lock:
            self.values[key] = value


async def demo_stream(event_type):
    """Stand-in for a Binance websocket subscription."""
    sequence = 0
    while True:
        await asyncio.sleep(0.25)
        sequence += 1
        yield {"type": event_type, "sequence": sequence}


class TradeStream(UniteIO, prefix="TRD"):
    def __init__(self, state):
        super().__init__(state=state)

    async def __call__(self) -> None:
        async for trade in demo_stream("trade"):
            self.state.update("last_trade", trade)


class TickerStream(UniteIO, prefix="TCK"):
    def __init__(self, state):
        super().__init__(state=state)

    async def __call__(self) -> None:
        async for ticker in demo_stream("ticker"):
            self.state.update("last_ticker", ticker)


state = MarketState()
trades = TradeStream(state=state)
ticker = TickerStream(state=state)

# Coroutine work is routed to the selected application's own event loop.
future = trades.submit(asyncio.sleep, 0, result="ready", name="warmup")
assert future.result() == "ready"

# Synchronous work runs on the shared UIOPool.
assert ticker.submit(sum, [1, 2, 3]).result() == 6

trades.stop()
ticker.stop()
```

`TradeStream` and `TickerStream` are different concrete application classes,
so they own different threads and different asyncio loops. On free-threaded
CPython, eligible Python work in those threads can execute simultaneously.
Network I/O remains asynchronous, and shared mutable objects still require
normal thread-safety measures such as locks.

Calling `UIOPool().shutdown()` stops all registered applications before
shutting down the shared executor.

## Documentation

The full guide and API reference are available at
[uniteio.readthedocs.io](https://uniteio.readthedocs.io/en/latest/).

Build the documentation locally with:

```console
python -m sphinx -W --keep-going -b html docs docs/_build/html
```

## Development

Create or activate an environment, then install the project with development
dependencies:

```console
uv pip install -e ".[dev]"
python -m pytest
```

Build and validate the release artifacts:

```console
uv build
uvx twine check dist/*
```

## License

UniteIO is distributed under the
[MIT License](https://github.com/dangreb/UniteIO/blob/master/LICENSE).

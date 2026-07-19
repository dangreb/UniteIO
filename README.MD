# UniteIO

[![Documentation](https://readthedocs.org/projects/uniteio/badge/?version=latest)](https://uniteio.readthedocs.io/en/latest/)
[![CI](https://github.com/dangreb/UniteIO/actions/workflows/ci.yml/badge.svg)](https://github.com/dangreb/UniteIO/actions/workflows/ci.yml)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/dangreb/UniteIO/blob/master/LICENSE)

UniteIO runs singleton applications on dedicated asyncio event-loop threads
while sharing one thread-pool executor for synchronous work.

> **Status:** UniteIO is an early alpha designed for CPython free-threaded
> environments. It requires Python 3.14.5 or newer and emits a warning when the
> interpreter's GIL is enabled.

## Installation

```console
pip install uniteio
```

## Quick start

```python
import asyncio

from uniteio import UniteIO


class Service(UniteIO, prefix="SVC"):
    def __init__(self, endpoint: str):
        super().__init__(endpoint=endpoint)

    async def __call__(self) -> None:
        await asyncio.Event().wait()


service = Service(endpoint="local")

# Coroutine work runs on the application's event loop.
future = service.submit(asyncio.sleep, 0, result="ready", name="warmup")
assert future.result() == "ready"

# Synchronous work runs on the shared UIOPool.
assert service.submit(sum, [1, 2, 3]).result() == 6

# Stop this app without shutting down the shared executor.
service.stop()
```

Each concrete `UniteIO` subclass has one active instance and its own asyncio
loop. Calling `UIOPool().shutdown()` stops all registered applications before
shutting down the executor.

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

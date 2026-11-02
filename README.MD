# UniteIO

[![Documentation](https://readthedocs.org/projects/uniteio/badge/?version=latest)](https://uniteio.readthedocs.io/en/latest/)
[![CI](https://github.com/dangreb/UniteIO/actions/workflows/ci.yml/badge.svg)](https://github.com/dangreb/UniteIO/actions/workflows/ci.yml)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/dangreb/UniteIO/blob/master/LICENSE)

UniteIO runs several asyncio applications in one Python process, each on its
own thread with its own event loop. The applications share the process, its
state, and one thread-pool executor for synchronous work.

It works with and without the GIL. On a free-threaded build, Python code on
different application threads can run in parallel; with the GIL, the loops are
still independent, but their Python code takes turns.

> **Status:** early alpha. Requires Python 3.14.5 or newer. The API may still
> change between minor versions.

## Installation

```console
pip install uniteio
```

## Example

```python
import asyncio
from uniteio import App, Runtime


class Clock(App):
    """Ticks on its own thread and loop."""

    def __init__(self, interval):
        self.interval = interval   # ordinary constructor, any signature
        self.ticks = 0

    async def __call__(self):
        while True:
            await asyncio.sleep(self.interval)
            self.ticks += 1


async def double(value):
    return value * 2


with Runtime() as rt:
    clock = rt.start(Clock(0.1))       # starts the thread and loop, returns the same object
    assert rt["Clock"] is clock and rt[Clock] is clock

    # From any thread: coroutines run on the app's loop, callables on the shared executor.
    assert clock.submit(double, 21).result(timeout=2) == 42
    assert clock.submit(sum, [1, 2, 3]).result(timeout=2) == 6
# leaving the block stops every application, then shuts the executor down
```

- `Runtime.start` runs `__call__` on the application's loop and returns once the
  loop is running. Several instances of one class need different names:
  `rt.start(Clock(1), name="slow")`.
- `App.submit` is how other threads hand work to an application. Inside the
  loop, use asyncio directly (`await`, `create_task`, `TaskGroup`).
- Application loops reject non-thread-safe calls (`call_soon`, `create_task`, …)
  coming from other threads, and blocking on loop work from the loop's own
  thread raises instead of deadlocking.

The [documentation](https://uniteio.readthedocs.io/en/latest/) covers errors,
shutdown, and the rules for crossing threads.

## Development

```console
uv pip install -e ".[dev]"
python -m pytest
python -m sphinx -W --keep-going -b html docs docs/_build/html
```

Build and validate the release artifacts:

```console
uv build
uvx twine check dist/*
```

## License

UniteIO is distributed under the
[MIT License](https://github.com/dangreb/UniteIO/blob/master/LICENSE).

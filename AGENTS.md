# AGENTS.md

## Project overview

UniteIO is an early-alpha Python library for running independent `asyncio`
applications on dedicated threads while sharing one process-wide thread pool.
It targets CPython 3.14.5+ and is designed primarily for free-threaded builds.

## Repository layout

- `uniteio/`: public package and concurrency implementation.
- `test/`: pytest suite; shared singleton cleanup lives in `test/conftest.py`.
- `docs/`: Sphinx documentation.
- `.github/workflows/`: CI and release behavior.
- `pyproject.toml`: package metadata and dependency groups.

## Environment and commands

Use Python 3.14.5 or newer. Install the editable development environment with:

```console
uv pip install -e ".[dev]"
```

Run the full test suite:

```console
python -m pytest
```

Run a focused test while developing:

```console
python -m pytest test/test_application.py
python -m pytest test/test_application.py::test_each_application_class_is_a_singleton
```

Match CI coverage checks with:

```console
python -m pytest --cov=uniteio --cov-report=term-missing
```

Validate documentation and distributions when those areas change:

```console
python -m sphinx -W --keep-going -b html docs docs/_build/html
python -m build
python -m twine check --strict dist/*
```

No formatter, linter, or static type checker is currently configured. Do not
introduce or claim a formatting requirement without updating project tooling.

## Implementation conventions

- Preserve the existing public API exported from `uniteio/__init__.py` and the
  module-level `__all__` declarations unless an API change is intentional.
- Keep thread ownership and event-loop affinity explicit. Coroutine work belongs
  on an application's loop; synchronous work belongs in the shared `UIOPool`.
- Treat shared mutable state as thread-sensitive and protect it with appropriate
  synchronization.
- Preserve singleton lifecycle behavior. Applications and the shared executor
  must be removable after shutdown so later tests or callers can recreate them.
- Avoid blocking an event-loop thread. Use asynchronous primitives on loops and
  thread-safe scheduling when crossing loop or thread boundaries.
- Maintain compatibility with both standard and free-threaded CPython 3.14.
- Follow the surrounding style: four-space indentation, descriptive snake_case
  tests, type annotations where they clarify contracts, and docstrings for
  public classes and methods.

## Testing expectations

- Add or update tests for every behavior change and regression fix.
- Concurrency tests must use bounded waits/timeouts so failures cannot hang CI.
- Do not rely on test order. The autouse fixture resets application and pool
  singleton state after each test; extend that cleanup if new global state is
  introduced.
- Exercise shutdown, cancellation, exception, and cross-thread edge cases when
  changing lifecycle or emitter behavior.
- Run the focused tests first, then the full suite before considering a change
  complete.

## Documentation and release notes

- Update `README.MD` for user-facing setup or introductory API changes.
- Update the relevant files in `docs/` for public behavior and API changes.
- Keep examples safe for concurrent execution and explicit about synchronization.
- Follow `RELEASING.md` for versioning and publication. Release tags must match
  the version in `pyproject.toml`.

## Change discipline

- Preserve unrelated working-tree changes; never reset or overwrite them.
- Keep changes narrowly scoped and avoid committing generated output such as
  coverage files, build artifacts, caches, or `docs/_build/`.
- Before changing concurrency semantics, read the implementation and its tests
  together and document any user-visible compatibility impact.

import asyncio
import threading

import pytest

from uio import UniteIO, UIOPool


class ConfiguredApplication(UniteIO):
    def __init__(self, enabled=False):
        self.started = threading.Event()
        super().__init__(enabled=enabled)

    async def __call__(self):
        self.started.set()
        await asyncio.Event().wait()


class OtherApplication(UniteIO):
    def __init__(self):
        self.started = threading.Event()
        super().__init__()

    async def __call__(self):
        self.started.set()
        await asyncio.Event().wait()


class NamedApplication(UniteIO, name="Named worker", prefix="NW"):
    def __init__(self):
        self.started = threading.Event()
        super().__init__()

    async def __call__(self):
        self.started.set()
        await asyncio.Event().wait()


def test_parameters_are_available_by_attribute_and_item():
    app = ConfiguredApplication(enabled=True)

    assert app.started.wait(2)
    assert app.enabled is True
    assert app["enabled"] is True
    with pytest.raises(KeyError):
        app["missing"]


def test_each_application_class_is_a_singleton():
    first = ConfiguredApplication(enabled=True)
    second = ConfiguredApplication(enabled=False)

    assert first is second
    assert second.enabled is True


def test_application_classes_share_pool_but_have_distinct_loops():
    first = ConfiguredApplication()
    second = OtherApplication()

    assert first.started.wait(2)
    assert second.started.wait(2)
    assert first.pool is second.pool is UIOPool()
    assert first.loop is not second.loop


def test_subclass_name_and_prefix_are_applied():
    app = NamedApplication()

    assert app.started.wait(2)
    assert app.name == "UIO:Named worker"
    assert app.taskname().startswith("NW:")


def test_concurrent_construction_returns_one_started_instance():
    workers = []
    results = []
    errors = []
    result_lock = threading.Lock()

    def construct():
        try:
            instance = ConfiguredApplication(enabled=True)
            with result_lock:
                results.append(instance)
        except BaseException as exc:
            with result_lock:
                errors.append(exc)

    for _ in range(8):
        worker = threading.Thread(target=construct)
        workers.append(worker)
        worker.start()
    for worker in workers:
        worker.join(2)

    assert not errors
    assert len(results) == 8
    assert len({id(instance) for instance in results}) == 1
    assert results[0].started.wait(2)


def test_varargs_constructor_is_rejected_at_class_definition():
    with pytest.raises(RuntimeError, match="no varargs"):

        class InvalidVarargsApplication(UniteIO):
            def __init__(self, *args):
                super().__init__()

            async def __call__(self):
                pass


def test_missing_uniteio_constructor_is_rejected():
    class MissingSuperApplication(UniteIO):
        def __init__(self):
            pass

        async def __call__(self):
            pass

    with pytest.raises(RuntimeError, match="super constructor"):
        MissingSuperApplication()


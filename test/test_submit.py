import asyncio
import contextvars
import threading

import pytest

from uio import UniteIO


marker = contextvars.ContextVar("marker", default="default")


class SubmissionApplication(UniteIO, prefix="SUB"):
    def __init__(self):
        self.started = threading.Event()
        super().__init__()

    async def __call__(self):
        self.started.set()
        await asyncio.Event().wait()


async def task_details(value=None):
    return value, asyncio.current_task().get_name(), marker.get()


def start_app():
    app = SubmissionApplication()
    assert app.started.wait(2)
    return app


def test_submit_precreated_coroutine_with_explicit_name():
    app = start_app()

    value, name, context_value = app.submit(
        task_details("value"), name="chosen-name"
    ).result(2)

    assert value == "value"
    assert name == "chosen-name"
    assert context_value == "default"


def test_submit_coroutine_function_with_args_and_kwargs():
    app = start_app()

    result = app.submit(task_details, value="from-kwargs", name="function").result(2)

    assert result[:2] == ("from-kwargs", "function")


def test_submit_uses_supplied_context():
    app = start_app()
    context = contextvars.copy_context()
    context.run(marker.set, "custom")

    result = app.submit(task_details(), context=context, name="context-task").result(2)

    assert result == (None, "context-task", "custom")


def test_automatic_task_names_are_unique_and_prefixed():
    app = start_app()

    first_name = app.submit(task_details()).result(2)[1]
    second_name = app.submit(task_details()).result(2)[1]

    assert first_name.startswith("SUB:")
    assert second_name.startswith("SUB:")
    assert first_name != second_name


def test_submit_callable_uses_shared_executor():
    app = start_app()

    def work(left, *, right):
        return left + right, threading.current_thread().name

    result, thread_name = app.submit(work, 20, right=22).result(2)

    assert result == 42
    assert thread_name.startswith("UIO:")


@pytest.mark.parametrize("target", [None, 1, object()])
def test_submit_rejects_invalid_targets(target):
    app = start_app()

    with pytest.raises(TypeError, match="Target must be"):
        app.submit(target)


def test_batch_accepts_list_and_returns_results_with_child_names():
    app = start_app()
    coroutines = [task_details(1), task_details(2)]

    results = app.submit(coroutines, name="batch").result(2)

    assert list(results.keys()) == coroutines
    assert list(results.values()) == [
        (1, "batch:0", "default"),
        (2, "batch:1", "default"),
    ]


def test_batch_accepts_one_shot_generator():
    app = start_app()

    results = app.submit(
        (task_details(value) for value in range(3)),
        name="generated",
    ).result(2)

    assert [value[0] for value in results.values()] == [0, 1, 2]
    assert [value[1] for value in results.values()] == [
        "generated:0",
        "generated:1",
        "generated:2",
    ]


def test_empty_batch_returns_empty_mapping():
    app = start_app()

    assert app.submit([], name="empty").result(2) == {}


def test_batch_propagates_exception_group_and_cancels_siblings():
    app = start_app()
    sibling_cancelled = threading.Event()

    async def fail():
        await asyncio.sleep(0)
        raise ValueError("boom")

    async def sibling():
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    future = app.submit([fail(), sibling()], name="failing-batch")

    with pytest.raises(ExceptionGroup) as raised:
        future.result(2)
    assert any(isinstance(exc, ValueError) for exc in raised.value.exceptions)
    assert sibling_cancelled.wait(2)

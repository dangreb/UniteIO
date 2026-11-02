import asyncio
import contextvars
import sys
import threading
import time

import pytest

from uniteio import App, Runtime, runtime


class Idle(App):
    name = "idle"

    async def __call__(self):
        await asyncio.Event().wait()


class Configured(App):
    def __init__(self, value, *, label="x"):
        self.value = value
        self.label = label
        self.seen_in_call = None

    async def __call__(self):
        # Constructor attributes exist before the entry point runs.
        self.seen_in_call = (self.value, self.label, threading.current_thread().name)


class Failing(App):
    async def __call__(self):
        raise ValueError("boom")


def test_start_returns_the_same_instance_and_registers_it():
    with Runtime() as rt:
        idle = rt.start(Idle())
        assert isinstance(idle, Idle)
        assert rt[Idle] is idle
        assert rt["idle"] is idle
        assert rt.get("idle") is idle
        assert rt.get("missing") is None
        assert idle in rt and len(rt) == 1


def test_constructor_is_ordinary_and_runs_before_entry_point():
    with Runtime() as rt:
        app = rt.start(Configured(42, label="y"))
        app.submit(asyncio.sleep, 0).result(2)
        assert app.seen_in_call == (42, "y", "UIO:Configured")


def test_each_app_has_its_own_loop_and_thread():
    with Runtime() as rt:
        first = rt.start(Idle())
        second = rt.start(Idle(), name="idle-2")
        assert second.name == "idle-2" and rt["idle-2"] is second
        assert first.loop is not second.loop
        assert first._thread is not second._thread
        with pytest.raises(KeyError):
            rt[Idle]  # two instances: lookup by class is ambiguous


def test_duplicate_name_and_double_start_are_rejected():
    with Runtime() as rt:
        idle = rt.start(Idle())
        with pytest.raises(ValueError):
            rt.start(Idle())
        with pytest.raises(RuntimeError):
            rt.start(idle)
        with Runtime() as other, pytest.raises(RuntimeError):
            other.start(idle)


def test_loop_budget_teaches_instead_of_scaling():
    with Runtime(max_loops=1) as rt:
        rt.start(Idle())
        extra = Idle()
        with pytest.raises(RuntimeError, match="topology"):
            rt.start(extra, name="idle-2")
        assert not extra.started and len(rt) == 1


def test_budget_can_be_disabled():
    with Runtime(max_loops=None) as rt:
        for i in range(3):
            rt.start(Idle(), name=f"idle-{i}")
        assert len(rt) == 3


def test_failing_entry_point_reaches_exception_handler():
    seen = []

    class Reporting(Failing):
        def exception_handler(self, loop, context):
            seen.append(context["exception"])

    with Runtime() as rt:
        app = rt.start(Reporting())
        app.submit(asyncio.sleep, 0.05).result(2)
    assert len(seen) == 1 and isinstance(seen[0], ValueError)


def test_loop_startup_failure_propagates_and_registers_nothing(monkeypatch):
    def broken(prefix):
        raise OSError("no loop for you")

    monkeypatch.setattr(runtime, "GuardedLoop", broken)
    with Runtime() as rt:
        idle = Idle()
        with pytest.raises(OSError, match="no loop"):
            rt.start(idle, name="temporary")
        assert len(rt) == 0 and not idle.started
        # Everything is back to the pre-start defaults, including the name.
        assert idle.name == "idle" and "name" not in vars(idle)
        assert idle.runtime is idle.loop is idle.thread is None


def test_name_defaults_to_class_name():
    with Runtime() as rt:
        app = rt.start(Configured(1))
        assert app.name == "Configured" and rt["Configured"] is app
        assert Configured.name is None  # resolved on the instance, the class is untouched


def test_runtime_attributes_are_none_and_methods_explain_before_start():
    idle = Idle()
    assert not idle.started and not idle.is_alive()
    assert idle.runtime is idle.loop is idle.thread is None
    for call in (lambda: idle.submit(asyncio.sleep, 0), idle.stop, idle.join):
        with pytest.raises(RuntimeError, match="not started"):
            call()


def test_join_and_is_alive_follow_the_thread():
    with Runtime() as rt:
        idle = rt.start(Idle())
        assert idle.is_alive() and idle.thread.name == "UIO:idle"
        idle.join(timeout=0.01)
        assert idle.is_alive()
        idle.submit(idle.stop)  # stop requested from the executor, not the app thread
        idle.join(timeout=2)
        assert not idle.is_alive()


def test_submit_routes_coroutines_to_loop_and_callables_to_executor():
    async def where(tag):
        return tag, threading.current_thread().name

    def add(left, *, right):
        return left + right, threading.current_thread().name

    with Runtime() as rt:
        idle = rt.start(Idle())
        assert idle.submit(where, "fn").result(2) == ("fn", "UIO:idle")
        assert idle.submit(where("obj")).result(2) == ("obj", "UIO:idle")
        total, thread = idle.submit(add, 20, right=22).result(2)
        assert total == 42 and thread.startswith("UIO_")


def test_loop_close_does_not_shut_shared_executor_down():
    with Runtime() as rt:
        first = rt.start(Idle())
        first.submit(asyncio.to_thread, sum, [1, 2]).result(2)
        assert first.stop(timeout=2)
        assert rt.executor.submit(sum, [3, 4]).result(2) == 7


def test_stop_unregisters_and_frees_the_name():
    with Runtime() as rt:
        idle = rt.start(Idle())
        assert idle.stop(timeout=2)
        assert rt.get("idle") is None
        rt.start(Idle())


def test_context_manager_closes_and_does_not_swallow_errors():
    with pytest.raises(LookupError):
        with Runtime() as rt:
            idle = rt.start(Idle())
            raise LookupError("propagates")
    assert not idle._thread.is_alive()
    with pytest.raises(RuntimeError, match="closed"):
        rt.start(Idle())


def test_close_from_app_thread_is_rejected():
    with Runtime() as rt:
        idle = rt.start(Idle())

        async def close_inside():
            rt.close()

        with pytest.raises(RuntimeError, match="application thread"):
            idle.submit(close_inside).result(2)


def test_default_runtime_is_recreated_after_close():
    first = Runtime.default()
    assert Runtime.default() is first
    first.close()
    second = Runtime.default()
    assert second is not first
    second.close()


# --- thread guard --------------------------------------------------------------------------

def test_non_thread_safe_loop_calls_from_another_thread_are_rejected():
    with Runtime() as rt:
        idle = rt.start(Idle())
        loop = idle.loop
        for call in (lambda: loop.call_soon(print), lambda: loop.call_later(1, print),
                     lambda: loop.call_at(loop.time() + 1, print)):
            with pytest.raises(RuntimeError, match="another thread"):
                call()
        coro = asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="another thread"):
            loop.create_task(coro)
        coro.close()
        # The thread-safe door stays open.
        done = threading.Event()
        loop.call_soon_threadsafe(done.set)
        assert done.wait(2)


def test_guarded_loop_runs_subprocesses_and_executor_work():
    async def child():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "print('ok')", stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        return out.strip(), await asyncio.to_thread(sum, [1, 2])

    with Runtime() as rt:
        idle = rt.start(Idle())
        assert idle.submit(child).result(10) == (b"ok", 3)


# --- submit --------------------------------------------------------------------------------

def test_submit_task_options_name_and_context():
    var = contextvars.ContextVar("var", default="unset")

    async def probe():
        return asyncio.current_task().get_name(), var.get()

    context = contextvars.copy_context()
    context.run(var.set, "from-context")
    with Runtime() as rt:
        idle = rt.start(Idle())
        assert idle.submit(probe, name="probe", context=context).result(2) == ("probe", "from-context")


def test_unnamed_tasks_carry_the_app_name():
    async def names():
        inner = asyncio.create_task(asyncio.sleep(0))
        async with asyncio.TaskGroup() as group:
            child = group.create_task(asyncio.sleep(0))
        await inner
        return asyncio.current_task().get_name(), inner.get_name(), child.get_name()

    with Runtime() as rt:
        idle = rt.start(Idle(), name="feed")
        own, inner, child = idle.submit(names).result(2)
        assert all(n.startswith("feed:") for n in (own, inner, child))
        assert len({own, inner, child}) == 3
        assert idle.submit(names, name="explicit").result(2)[0] == "explicit"


def test_eager_submit_takes_its_first_step_inside_the_spawn_callback():
    order = []
    gate = threading.Event()

    async def hold_loop():
        gate.wait(2)  # blocks the loop on purpose, so both submits queue up

    async def record(tag):
        order.append(tag)

    with Runtime() as rt:
        idle = rt.start(Idle())
        held = idle.submit(hold_loop)
        lazy = idle.submit(record, "lazy")
        eager = idle.submit(record, "eager", eager=True)
        gate.set()
        for future in (held, lazy, eager):
            future.result(2)
    assert order == ["eager", "lazy"]


def test_submit_batch_runs_as_one_task_group():
    async def double(value):
        return asyncio.current_task().get_name(), value * 2

    with Runtime() as rt:
        idle = rt.start(Idle())
        coros = [double(1), double(2)]
        result = idle.submit(coros, name="batch").result(2)
        assert list(result) == coros
        assert list(result.values()) == [("batch:0", 2), ("batch:1", 4)]


def test_submit_batch_failure_cancels_siblings_and_raises_group():
    cancelled = threading.Event()

    async def slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def fail():
        raise ValueError("boom")

    with Runtime() as rt:
        idle = rt.start(Idle())
        with pytest.raises(ExceptionGroup) as caught:
            idle.submit([slow(), fail()]).result(2)
        assert caught.group_contains(ValueError) and cancelled.is_set()


def test_submit_rejects_bad_batches_and_closes_their_coroutines():
    with Runtime() as rt:
        idle = rt.start(Idle())
        coro = asyncio.sleep(0)
        with pytest.raises(TypeError, match="batch"):
            idle.submit([coro, "not a coroutine"])
        assert coro.cr_frame is None  # closed: no "never awaited" warning
        with pytest.raises(TypeError, match="loop work only"):
            idle.submit(sum, [1], name="sum")
        with pytest.raises(TypeError):
            idle.submit(42)


def test_blocking_on_loop_work_from_its_own_thread_raises_instead_of_deadlocking():
    async def wait_on_myself(app):
        return app.submit(asyncio.sleep, 0).result(1)

    with Runtime() as rt:
        idle = rt.start(Idle())
        with pytest.raises(RuntimeError, match="deadlock"):
            idle.submit(wait_on_myself, idle).result(2)


def test_another_loop_awaits_loop_work_with_wrap_future():
    async def ask(other):
        return await asyncio.wrap_future(other.submit(asyncio.sleep, 0, "answer"))

    with Runtime() as rt:
        first = rt.start(Idle())
        second = rt.start(Idle(), name="idle-2")
        assert first.submit(ask, second).result(2) == "answer"


def test_cancelling_the_future_cancels_the_task():
    cancelled = threading.Event()

    async def slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with Runtime() as rt:
        idle = rt.start(Idle())
        future = idle.submit(slow)
        time.sleep(0.05)
        assert future.cancel()
        assert cancelled.wait(2)


def test_stop_settles_pending_work_and_later_submits_are_refused():
    with Runtime() as rt:
        idle = rt.start(Idle())
        pending = idle.submit(asyncio.sleep, 10)
        time.sleep(0.05)
        assert idle.stop(timeout=2)
        assert pending.cancelled()
        with pytest.raises(RuntimeError):
            idle.submit(asyncio.sleep, 0)

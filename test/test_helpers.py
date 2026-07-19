import asyncio
import threading

from uniteio import UniteIO


class HelperApplication(UniteIO):
    def __init__(self):
        self.started = threading.Event()
        super().__init__()

    async def __call__(self):
        self.started.set()
        await asyncio.Event().wait()


def test_sleep_helpers_return_requested_result():
    app = HelperApplication()
    assert app.started.wait(2)

    assert app.sleep(0, "sync-result") == "sync-result"
    assert app.submit(app.asleep, 0, result="async-result").result(2) == "async-result"


def test_sleep_helpers_default_to_application():
    app = HelperApplication()
    assert app.started.wait(2)

    assert app.sleep(0) is app
    assert app.submit(app.asleep, 0).result(2) is app


def test_exception_handler_records_and_delegates_context():
    app = HelperApplication()
    assert app.started.wait(2)
    delegated = []

    class LoopStub:
        def default_exception_handler(self, context):
            delegated.append(context)

    context = {"message": "test error"}
    app.exception_handler(LoopStub(), context)

    assert app._except == [context]
    assert delegated == [context]

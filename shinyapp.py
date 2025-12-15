
import os
import sys
import loguru
import asyncio
import threading

from starlette.types import ASGIApp
from abc import ABCMeta, abstractmethod
from shiny import App, render, ui, run_app
from concurrent.futures import ThreadPoolExecutor
from shinyswatch import theme, theme_picker_ui, theme_picker_server



class _start(ABCMeta, type(threading.Thread)):
    __instance__: dict[type ,Self] = dict()
    def __new__(mcls, name: str, bases: list[type], namespace: dict[str, Any], **kwargs):
        return super().__new__(mcls, name, bases, namespace, **kwargs)
    def __app__(cls, *args, **kwargs):
        return cls.__instance__.get(cls, None)
    def __call__(cls):
        return cls.__instance__.get(cls, None) or cls.__instance__.setdefault(cls, super(_start, cls).__call__(
            target=run_app,
            name=f"{cls.__name__} Backend",
            options=dict(factory=True), ## Will do ClassArgs based interface maybe
            daemon=True
        )).start()


class UniteIO(threading.Thread, metaclass=_start):

    loop: asyncio.AbstractEventLoop = None
    pool: ThreadPoolExecutor = None
    app: App = None

    def __init__(self, options: dict, **kwargs):
        super(UniteIO, self).__init__(kwargs=dict(app=self, **options, loop=options.pop("loop", self.new_loop)), **kwargs)
        pass

    def new_loop(self) -> asyncio.AbstractEventLoop:
        self.pool: ThreadPoolExecutor = ThreadPoolExecutor(thread_name_prefix=f'ShinyUIO:Pool', max_workers=os.cpu_count()//2)
        self.loop = asyncio.new_event_loop()
        self.loop.set_default_executor(self.pool)
        return self.loop

    @abstractmethod
    def __call__(self) -> ASGIApp:...


class ShinyUIO(UniteIO):
    def __call__(self) -> ASGIApp:
        super(ShinyUIO, self).__call__()
        app_ui = ui.page_fixed(
            theme_picker_ui(),
            ui.input_action_button("sumbit", "Submit"),
            ui.output_text("counter"),
            theme=theme.darkly
        )

        def server(_i,_o, session):
            theme_picker_server()
            @render.text
            def slider_val():
                return f"Slider value: {session.input.val()}"

        self.app = App(app_ui, server)
        return self.app


def main():
    loguru.logger.add("app.log", rotation="10 MB")
    loguru.logger.info("Starting Maybe Shiny Experiment with UIO Infrastructure")

    #print(str(sys._is_gil_enabled().__qualname__), sys._is_gil_enabled())


    aio = ShinyUIO()
    halt = threading.Event()
    while not halt.wait(timeout=2.0):
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)

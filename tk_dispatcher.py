"""Desktop owner-thread bridge. No Tk root is created by importing this module."""
import threading
from calendar_chat_tools import fail


class TkDispatcher:
    def __init__(self, app):
        self.app, self.owner = app, threading.get_ident()

    def __call__(self, function):
        from concurrent.futures import Future, TimeoutError
        if threading.get_ident() == self.owner:
            return function()
        future = Future()
        def run():
            if self.app.lifecycle.exiting or self.app.lifecycle.stopped:
                future.cancel()
            elif future.set_running_or_notify_cancel():
                try:
                    future.set_result(function())
                except Exception as error:
                    future.set_exception(error)
        if self.app.lifecycle.exiting or self.app.lifecycle.stopped:
            fail('月历正在退出。', 'service_stopped')
        self.app._call_main(run)
        try:
            return future.result(timeout=10)
        except TimeoutError:
            future.cancel()
            fail('主窗口暂不可用，请稍后查询操作结果。', 'service_unavailable')

"""Single-owner execution without Tk or platform APIs. Never retries a callable."""
from concurrent.futures import Future, TimeoutError, CancelledError
from queue import Queue
import threading

from desktop_calendar.assistant.calendar_chat_tools import ChatError


class SerialExecutor:
    def __init__(self, timeout=10):
        self.timeout = timeout
        self._queue = Queue()
        self._lock = threading.Lock()
        self._accepting = True
        self.owner = None
        self.thread = threading.Thread(target=self._run, name='calendar-business', daemon=True)
        self.thread.start()

    def _run(self):
        self.owner = threading.get_ident()
        while True:
            task = self._queue.get()
            if task is None:
                return
            future, function = task
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(function())
            except BaseException as exc:
                future.set_exception(exc)

    def __call__(self, function):
        if threading.get_ident() == self.owner:
            return function()  # Reentrant service calls within the owning transaction.
        future = Future()
        with self._lock:
            if not self._accepting:
                raise ChatError('service_stopped', '业务执行器已停止接收操作。')
            self._queue.put((future, function))
        try:
            return future.result(timeout=self.timeout)
        except CancelledError:
            raise ChatError('service_stopped', '操作尚未开始，已随关闭取消。') from None
        except TimeoutError:
            if future.done():
                return future.result()  # A callable may itself raise TimeoutError.
            if future.cancel():
                raise ChatError('service_unavailable', '操作等待超时，未开始执行。') from None
            raise ChatError('execution_uncertain', '操作仍在执行，请查询结果，不要重复提交。') from None

    def stop(self, timeout=10):
        with self._lock:
            if self._accepting:
                self._accepting = False
                # Cancel work that has not started. A running save must finish.
                while not self._queue.empty():
                    task = self._queue.get_nowait()
                    if task is not None:
                        task[0].cancel()
                self._queue.put(None)
        if threading.get_ident() == self.owner:
            return
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise ChatError('execution_uncertain', '业务操作尚未结束，关闭未完成。')

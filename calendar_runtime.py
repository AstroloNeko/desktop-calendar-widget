"""Composition roots shared by desktop and isolated/headless callers.

Importing this module creates no root, thread, Win32 handle, file or server.
Production credentials default to DPAPI; alternative protectors must be explicit.
"""
from copy import deepcopy
from pathlib import Path
import time
import threading

from calendar_core import Store
from event_commands import EventCommandHistory
from calendar_event_service import CalendarEventService
from calendar_mutation_service import CalendarMutationService
from calendar_action_gateway import CalendarActionGateway
from desktop_calendar.assistant.calendar_chat_service import CalendarChatService
from desktop_calendar.assistant.calendar_chat_sessions import ChatJournal
from desktop_calendar.assistant.calendar_chat_http import LocalChatHTTP
from desktop_calendar.assistant.calendar_chat_tools import ChatError
from calendar_executor import SerialExecutor
from desktop_calendar.assistant.llm_provider import AIConfiguration, DeepSeekProvider


class CalendarRuntime:
    """Caller supplies ownership, refresh ports and existing desktop history."""
    def __init__(self, store, history, dispatch, *, configuration=None, journal=None,
                 provider=None, clock=time.time, refresh=lambda change: None,
                 habit_refresh=lambda: None, writable=lambda: True, repository=None):
        self.state = 'starting'
        self.http = None
        self.dispatch = dispatch
        if repository is not None and repository is not store.repository:
            raise ValueError('Repository must be the Store-owned adapter; inject its factory when creating Store.')
        self.repository = store.repository
        self.configuration = configuration or AIConfiguration(store.data_file.parent / 'ai-assistant')
        self.journal = journal or ChatJournal(self.configuration.folder / 'sessions.sqlite3', self.configuration.protector)
        self.gateway = CalendarActionGateway(store, history, dispatch, refresh,
            lambda: self.state == 'running' and not store.load_error and writable(), habit_refresh)
        self.events = CalendarEventService(history)
        self.mutations = CalendarMutationService(history)
        self.service = CalendarChatService(self.gateway, self.journal,
            provider if provider is not None else DeepSeekProvider(self.configuration), clock=clock)
        self.state = 'running'

    def apply_http(self, *, wait_requests=False, timeout=10):
        if self.state != 'running':
            raise ChatError('service_stopped', '运行时已停止。')
        config = self.configuration.load()
        if self.http:
            self.http.stop(wait_requests=wait_requests, timeout=timeout)
            self.http = None
        if config.get('http_enabled'):
            server = LocalChatHTTP(self.service, config['token'], config['port'])
            try:
                server.start()
            except OSError:
                raise ChatError('service_unavailable', '本机API端口不可用；请更换端口或关闭API。桌面聊天仍可使用。') from None
            self.http = server

    def stop(self, *, wait=False, timeout=10):
        if self.state == 'stopped':
            return
        self.state = 'stopping'
        self.service.stopped = True
        if self.http:
            self.http.stop(wait_requests=wait, timeout=timeout)
            self.http = None
        if wait:
            if not self.service.lock.acquire(timeout=timeout):
                raise ChatError('service_unavailable', '模型请求尚未结束，关闭未完成。')
            self.service.lock.release()
        # Journal opens/closes a SQLite connection per operation; no retained handle.
        self.state = 'stopped'


class _OwnedService:
    """Trusted in-process facade; arguments/results cannot leak canonical objects."""
    def __init__(self, runtime, service):
        self._runtime, self._service = runtime, service

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        target = getattr(self._service, name)
        if not callable(target):
            raise AttributeError(name)
        def invoke(*args, **kwargs):
            args, kwargs = deepcopy(args), deepcopy(kwargs)
            return self._runtime._call(lambda: target(*args, **kwargs))
        return invoke


class HeadlessRuntime:
    """Explicit path required; one Store, one history, one business owner thread.

    Chat/LLM runs on the caller (or existing HTTP handler), never on the business
    executor. Use events/mutations/query/snapshot/undo/redo instead of raw Store.
    """
    def __init__(self, data_file, *, provider=None, protector=None, configuration=None,
                 journal=None, clock=time.time, notification_backend=None, timeout=10, repository_factory=None):
        self.state = 'starting'
        self._stop_lock = threading.Lock()
        self.executor = SerialExecutor(timeout)
        self._core = None
        self._repository_closed = False
        try:
            def compose():
                self._store = Store(Path(data_file), repository_factory=repository_factory)
                self._history = EventCommandHistory(self._store)
                self._reminders = None
                config = configuration or AIConfiguration(self._store.data_file.parent / 'ai-assistant', protector)
                self._core = CalendarRuntime(self._store, self._history, self.executor,
                    configuration=config, journal=journal, provider=provider, clock=clock,
                    writable=lambda: self.state == 'running',
                    refresh=lambda change: self._invalidate_reminders(), habit_refresh=self._invalidate_reminders)
                if notification_backend is not None:
                    from reminder_engine import ReminderEngine
                    self._reminders = ReminderEngine(self._store, notification_backend)
            self.executor(compose)
            self.events = _OwnedService(self, self._core.events)
            self.mutations = _OwnedService(self, self._core.mutations)
            self.service = self._core.service
            self.state = 'running'
        except BaseException:
            self.state = 'failed'
            if hasattr(self, '_store'):
                self.executor(self._close_repository)
            self.executor.stop()
            raise

    def _call(self, function):
        if self.state != 'running':
            raise ChatError('service_stopped', '运行时未运行。')
        def owned():
            if self.state != 'running' or self._store.load_error:
                raise ChatError('service_unavailable', self._store.load_error or '运行时正在关闭。')
            result = function()
            if getattr(result, 'changed', False):
                self._invalidate_reminders()
            return deepcopy(result)
        return self.executor(owned)

    def query(self, arguments):
        return self._call(lambda: self._core.gateway.query(deepcopy(arguments)))

    def snapshot(self):
        from dataclasses import asdict
        def read():
            snapshot = self._core.repository.read_calendar_snapshot()
            return dict(events=[asdict(e) for e in snapshot['events']],
                routines=[asdict(r) for r in snapshot['routines']],
                categories=[asdict(c) for c in snapshot['categories']], date_states=snapshot['date_states'])
        return self._call(read)

    def undo(self):
        return self._call(lambda: self._history_change(self._history.undo))

    def redo(self):
        return self._call(lambda: self._history_change(self._history.redo))

    def _history_change(self, operation):
        result = operation()
        if result is not None:
            self._invalidate_reminders()
        return result

    def _invalidate_reminders(self):
        if self._reminders is not None:
            self._reminders.invalidate()

    def reminder_tick(self, now):
        if self._reminders is None:
            raise ValueError('No notification backend was supplied.')
        return self._call(lambda: self._reminders.tick(now))

    def apply_http(self):
        with self._stop_lock:
            if self.state != 'running':
                raise ChatError('service_stopped', '运行时未运行。')
            self._core.apply_http(wait_requests=True)
            return self._core.http

    def stop(self, timeout=10):
        with self._stop_lock:
            if self.state == 'stopped':
                return
            self.state = 'stopping'
            try:
                self._core.stop(wait=True, timeout=timeout)
            finally:
                try:
                    if not self._repository_closed:
                        self.executor(self._close_repository)
                finally:
                    self.executor.stop(timeout)
            self.state = 'stopped'

    def _close_repository(self):
        self._store.repository.close()
        self._repository_closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()

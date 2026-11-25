"""Desktop adapter. Remote work/service locks never block Tk's main thread."""
from datetime import datetime
from pathlib import Path
import secrets
import sqlite3
import threading
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

from tk_dispatcher import TkDispatcher
from desktop_calendar.assistant.calendar_chat_protocol import ChatError, envelope, event_line
from desktop_calendar.assistant.llm_provider import AIConfiguration
from desktop_calendar.reminders.reminder_ui import reminder_button
from desktop_calendar.ui.themed_controls import (RoundedCardShell, SettingsScroll, form_button,
                                                  form_entry, themed_check)
from desktop_calendar.updates.update_dialog import dialog_geometry
from desktop_calendar.remote_presentation import CONNECTION_LABELS, RemoteTrayConfig, model_status
from desktop_calendar.remote_channels import channel_name
from desktop_calendar.remote_service import RemoteStatus, test_connection
from desktop_calendar.remote_transport import SAFETY_CHECK_MINUTES, StatusError, Unavailable, normalize_url
from calendar_runtime import CalendarRuntime


class ChatRuntime:
    def __init__(self, app):
        self.app, self.window, self.http = app, None, None
        self.folder = app.store.data_file.parent / 'ai-assistant'
        self.configuration = AIConfiguration(self.folder)
        from desktop_calendar.assistant.calendar_chat_sessions import ChatJournal
        session = getattr(app, 'storage_session', None)
        journal = ChatJournal(session.journal_path, self.configuration.protector) if session else None
        self.core = CalendarRuntime(app.store, app.event_commands, TkDispatcher(app),
            configuration=self.configuration, journal=journal, refresh=app._refresh_event_change,
            writable=self.calendar_writable,
            habit_refresh=self.refresh_habits, on_remote_status_change=self.queue_tray_status)
        self.journal, self.gateway, self.service = self.core.journal, self.core.gateway, self.core.service
        self.jobs = threading.BoundedSemaphore(4)
        self.error = None
        try:
            self.apply_http()
        except ChatError as error:
            self.error = str(error)
        try:
            self.core.apply_remote()
        except (OSError, ValueError):
            self.error = '私人远程服务未启动，请检查 AI 设置。'
            self.queue_tray_status()

    def queue_tray_status(self):
        self.app._call_main(lambda: publish_remote_tray(self.app, self.core.remote_tray_status()))

    def refresh_habits(self):
        self.app.invalidate_reminders()
        self.app.render()
        manager = self.app.routine_manager
        if manager and manager.winfo_exists():
            manager.refresh()

    def calendar_writable(self):
        app = self.app
        if app.lifecycle.exiting or app.update_busy or app._lifecycle_operation_depth:
            return False
        modal = app._active_modal_window()
        return (modal is None or modal is self.window
                or isinstance(modal, AISettings) and modal.runtime is self)

    def apply_http(self):
        try:
            self.core.apply_http()
        finally:
            self.http = self.core.http

    def clear_ai_settings(self):
        try:
            return self.core.clear_ai_settings()
        finally:
            self.http = self.core.http

    def submit(self, function, callback):
        if self.service.stopped:
            return
        if not self.jobs.acquire(blocking=False):
            callback(envelope(status='failed', error_code='service_busy', error_message='AI请求较多，请稍后重试。'))
            return
        def run():
            try:
                result = function()
                result['sessions'] = self.service.list_sessions()
            except ChatError as error:
                result = envelope(status='failed', error_code=error.code, error_message=str(error))
            except Exception:
                result = envelope(status='failed', error_code='execution_failed', error_message='AI操作未完成，请查看操作状态或重新查询。')
            finally:
                self.jobs.release()
            if not self.service.stopped:
                self.app._call_main(lambda: callback(result) if not self.service.stopped else None)
        threading.Thread(target=run, daemon=True, name='calendar-chat-job').start()

    def stop(self):
        self.core.stop()
        self.http = None


def chat_runtime_for(app):
    runtime = getattr(app, 'chat_runtime', None)
    if runtime is None:
        try:
            runtime = ChatRuntime(app)
            app.chat_runtime = runtime
        except (OSError, ValueError):
            app._show_feedback('AI组件无法初始化；日历仍可正常使用，请检查本机配置目录。')
            return
    return runtime


def open_chat(app):
    blocker = app._active_modal_window()
    if blocker:
        app._focus_lifecycle_owner(blocker)
        return
    runtime = chat_runtime_for(app)
    if runtime is None:
        return
    if runtime.window and runtime.window.winfo_exists():
        runtime.window.lift()
        return
    runtime.window = ChatDialog(app, runtime)


def open_ai_settings(app):
    blocker = app._active_modal_window()
    if blocker:
        if isinstance(blocker, AISettings):
            blocker.tabs.select(blocker.remote_tab)
        app._focus_lifecycle_owner(blocker)
        return
    runtime = chat_runtime_for(app)
    if runtime is not None:
        return AISettings(app=app, runtime=runtime, show_remote=True)


def publish_remote_tray(app, status):
    if status == getattr(app, '_remote_tray_status', (None, '')):
        return
    app._remote_tray_status = status
    tray = getattr(app, 'tray_icon', None)
    if tray:
        tray.update_status(*status)


def start_chat_if_enabled(app):
    """Resume explicitly enabled adapters after desktop startup."""
    if getattr(app, 'chat_runtime', None) is not None or app.lifecycle.exiting or app.lifecycle.stopped:
        return  # A user may open AI before the delayed startup callback fires.
    folder = app.store.data_file.parent / 'ai-assistant'
    if not (folder / 'configuration.dpapi').exists():
        return
    try:
        config = AIConfiguration(folder).load()
        if config.get('http_enabled') or config.get('remote_enabled') is True:
            app.chat_runtime = ChatRuntime(app)
            if app.chat_runtime.error:
                app._show_feedback(app.chat_runtime.error)
        else:
            publish_remote_tray(app, RemoteTrayConfig.from_settings(config).tray_status(RemoteStatus()))
    except (OSError, ValueError):
        app._show_feedback('AI本机API未启动；日历仍可用，请检查AI设置。')


class _ChatWidgets:
    """One chat UI and set of callbacks, hosted by a dialog or the workspace."""

    _INPUT_HINT = 'Enter 发送\nShift+Enter 换行'
    _EMPTY_HINT = '还没有消息\n在下方输入，开始对话。'
    _STATUS_LABELS = {
        'pending': '等待确认', 'confirmation_required': '等待确认',
        'clarification_required': '请补充信息', 'confirmed': '已确认',
        'succeeded': '操作已完成', 'cancelled': '操作已取消',
        'failed': '本次操作未完成', 'expired': '操作已过期',
        'uncertain': '结果待核查，请勿重复执行',
    }

    def _embedded_button(self, parent, text, command, *, primary=False, quiet=False):
        theme, dp = self.app.theme, self.app.dpi.px
        surface = parent.cget('bg')
        fill = theme.accent if primary else surface if quiet else theme.control_background
        fg = theme.text_on_accent if primary else theme.control_text
        shell = RoundedCardShell(parent, theme, dp, fill, surface=surface,
                                 outline='') if theme.rounded_widget_shells else None
        button = tk.Button(shell or parent, text=text, command=command, font=(self.app.search_font, 8),
                           fg=fg, bg=fill, activeforeground=fg,
                           activebackground=theme.accent_hover if primary else theme.control_hover,
                           disabledforeground=theme.text_disabled, relief='flat', bd=0,
                           highlightthickness=0 if shell else dp(1), highlightbackground=fill,
                           highlightcolor=theme.input_focus, padx=dp(4), pady=dp(3), cursor='hand2')
        def paint(_event=None, hovered=False):
            disabled = str(button.cget('state')) == 'disabled'
            destructive = getattr(button, '_destructive', False)
            background = (theme.control_background if disabled else
                          button.cget('activebackground') if hovered else
                          theme.danger_soft if destructive else fill)
            button.configure(bg=background)
            border = theme.input_focus if button.focus_get() == button else theme.danger if destructive and not disabled else ''
            if shell:
                shell.set_fill(background)
                shell.set_outline(border)
            else:
                button.configure(highlightbackground=border or background)
        button.bind('<Enter>', lambda e: paint(e, True))
        button.bind('<Leave>', paint)
        button.bind('<FocusIn>', paint)
        button.bind('<FocusOut>', paint)
        if shell:
            button.pack(fill='both', expand=True, padx=dp(5), pady=dp(2))
        return button, shell or button, paint

    def _build_chat(self, app, runtime, width, *, embedded=False):
        self.app, self.runtime, self.session_id, self.operation, self.busy = app, runtime, None, None, False
        self._embedded = embedded
        theme, dp, font = app.theme, app.dpi.px, app.search_font
        surface = theme.workspace_detail if embedded else theme.panel_background
        self.configure(bg=surface)
        bar = tk.Frame(self, bg=surface)
        bar.pack(fill='x', padx=dp(12), pady=dp(3 if embedded else 6))
        new_session = lambda: self.request(self.runtime.service.new_session)
        self.sessions = ttk.Combobox(bar, state='readonly', width=1 if embedded else 10)
        if embedded:
            self.settings_button, settings, _ = self._embedded_button(bar, 'AI 设置', self.configure_ai, quiet=True)
            bar.columnconfigure(0, weight=1)
            self.sessions.grid(row=0, column=0, sticky='nsew', padx=(0, dp(4)))
            settings.grid(row=0, column=1, sticky='e')
            tools = tk.Frame(bar, bg=surface)
            tools.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(dp(3), 0))
            self.new_button, new, _ = self._embedded_button(tools, '＋ 新对话', new_session)
            self.clear_button, clear, _ = self._embedded_button(tools, '清除此对话', self.clear, quiet=True)
            new.pack(side='left')
            clear.pack(side='right')
            self._session_labels = []
            self._session_font = tkfont.Font(root=self, font=self.sessions.cget('font') or 'TkTextFont')
            self.sessions.bind('<Configure>', lambda _e: self._fit_session_labels(), add='+')
        else:
            self.settings_button = reminder_button(bar, app, '模型 / 本机API设置', self.configure_ai)
            self.new_button = reminder_button(bar, app, '新对话', new_session)
            self.clear_button = reminder_button(bar, app, '清除此对话', self.clear)
            self.settings_button.pack(side='right')
            self.new_button.pack(side='left')
            self.clear_button.pack(side='left', padx=dp(5))
            self.sessions.pack(side='left')
        self.sessions.bind('<<ComboboxSelected>>', self.load_session)
        bottom = tk.Frame(self, bg=surface)
        bottom.pack(side='bottom', fill='x', padx=dp(12), pady=dp(8))
        self.status = tk.StringVar(value=self._INPUT_HINT if embedded else '未启用远程模型。请先阅读隐私说明并配置自己的密钥。')
        status = tk.Label(bottom, textvariable=self.status, font=(font, 8), wraplength=width-dp(40), justify='left',
                          anchor='w' if embedded else 'center',
                          bg=surface, fg=theme.text_secondary, height=2 if embedded else 0)
        if not embedded:
            status.pack(fill='x')
        rounded = embedded and theme.rounded_widget_shells
        field = RoundedCardShell(bottom, theme, dp, theme.input_background, outline=theme.input_border,
                                 surface=surface, cursor='xterm') if rounded else bottom
        self.input = tk.Text(field, height=2 if embedded else 3, wrap='word', font=(font, 10),
                             bg=theme.input_background, fg=theme.text_primary, insertbackground=theme.text_primary,
                             undo=True, **(dict(width=1, bd=0, relief='flat', padx=dp(2), pady=dp(2),
                                 highlightthickness=0 if rounded else dp(1),
                                 highlightbackground=theme.input_border, highlightcolor=theme.input_focus) if embedded else {}))
        if rounded:
            self.input.pack(fill='both', expand=True, padx=dp(7), pady=dp(4))
            field.bind('<Button-1>', lambda _e: self.input.focus_set())
            self.input.bind('<FocusIn>', lambda _e: field.set_outline(theme.input_focus))
            self.input.bind('<FocusOut>', lambda _e: field.set_outline(theme.input_border))
        if not embedded:
            self.input.pack(fill='x', pady=dp(5))
        self.input.bind('<Return>', self.send)
        self.input.bind('<Shift-Return>', lambda _e: None)
        if embedded:
            def fit_composer(event):
                lines = 2 if event.height < dp(325) else 3
                if int(self.input.cget('height')) != lines:
                    self.input.configure(height=lines)
            self.bind('<Configure>', fit_composer, add='+')
        self.candidate = ttk.Combobox(bottom, state='readonly')
        actions = tk.Frame(bottom, bg=surface) if embedded else bottom
        if embedded:
            self.confirm_button, confirm, confirm_paint = self._embedded_button(actions, '确认此操作', self.confirm)
            self.cancel_button, cancel, cancel_paint = self._embedded_button(actions, '取消操作', self.cancel, quiet=True)
            self.send_button, send, send_paint = self._embedded_button(bottom, '发送', self.send, primary=True)
            self._action_paints = (confirm_paint, cancel_paint, send_paint)
            bottom.columnconfigure(0, weight=1)
            self.candidate.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, dp(4)))
            actions.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(0, dp(4)))
            actions.columnconfigure(0, weight=1)
            actions.columnconfigure(1, weight=1)
            confirm.grid(row=0, column=0, sticky='ew', padx=(0, dp(4)))
            cancel.grid(row=0, column=1, sticky='ew')
            self._pending_actions = actions
            self.candidate.grid_remove()
            actions.grid_remove()
            (field if rounded else self.input).grid(row=2, column=0, columnspan=2, sticky='ew', pady=(0, dp(4)))
            status.grid(row=3, column=0, sticky='ew', padx=(0, dp(4)))
            send.grid(row=3, column=1, sticky='se')
            status.bind('<Configure>', lambda e: status.configure(wraplength=e.width))
        else:
            self.confirm_button = reminder_button(actions, app, '确认此操作', self.confirm)
            self.cancel_button = reminder_button(actions, app, '取消操作', self.cancel)
            self.send_button = reminder_button(bottom, app, '发送', self.send, primary=True)
            self.send_button.pack(side='right')
            self.close_button = reminder_button(bottom, app, '关闭', self.close)
            self.close_button.pack(side='right', padx=dp(5))
            self.confirm_button.pack(side='left')
            self.cancel_button.pack(side='left', padx=dp(5))
            self.candidate.pack(fill='x', pady=dp(4))
        self.confirm_button.configure(state='disabled')
        self.cancel_button.configure(state='disabled')
        self.candidate.bind('<<ComboboxSelected>>', self.select_candidate)
        frame = tk.Frame(self, bg=surface)
        frame.pack(fill='both', expand=True, padx=dp(12))
        self.transcript = tk.Text(frame, wrap='word', font=(font, 9 if embedded else 10), bg=theme.workspace_detail, fg=theme.text_primary,
                                  relief='flat', state='disabled', padx=dp(8), pady=dp(6),
                                  **({'height': 1, 'width': 1} if embedded else {}))
        scroll = ttk.Scrollbar(frame, command=self.transcript.yview, style='Global.Vertical.TScrollbar')
        scroll.pack(side='right', fill='y')
        self.transcript.configure(yscrollcommand=scroll.set)
        self.transcript.pack(fill='both', expand=True)
        self._empty_transcript = False
        if embedded:
            self.transcript.tag_configure('empty', foreground=theme.text_secondary, justify='center', spacing1=dp(14))
            self.transcript.tag_configure('user', background=theme.accent_soft, lmargin1=dp(5), lmargin2=dp(5),
                                          rmargin=dp(4), spacing1=dp(4), spacing3=dp(4))
            self.transcript.tag_configure('assistant', spacing1=dp(3), spacing3=dp(3))
            self.transcript.tag_configure('system', foreground=theme.text_secondary)
            self.transcript.tag_configure('preview', background=theme.control_background, lmargin1=dp(5),
                                          lmargin2=dp(5), rmargin=dp(4), spacing1=dp(3), spacing3=dp(3))
            self.transcript.tag_configure('delete', background=theme.danger_soft)
            def scroll_if_needed(first, last):
                scroll.set(first, last)
                if float(first) == 0.0 and float(last) == 1.0:
                    if scroll.winfo_manager():
                        scroll.pack_forget()
                elif not scroll.winfo_manager():
                    scroll.pack(side='right', fill='y', before=self.transcript)
            self.transcript.configure(yscrollcommand=scroll_if_needed)

    def _fit_session_labels(self):
        # Keep full names in the dropdown; ellipsize only the entry display.
        if self.session_id not in getattr(self, 'session_ids', ()) or self.sessions.winfo_width() <= 1:
            return
        index = self.session_ids.index(self.session_id)
        label = self._session_labels[index]
        budget = max(0, self.sessions.winfo_width() - self.app.dpi.px(24))
        measure = self._session_font.measure
        if measure(label) > budget:
            end = min(len(label), budget)
            while end and measure(label[:end] + '…') > budget:
                end -= 1
            label = label[:end] + '…'
        self.sessions.set(label)

    def resume(self, session_id=None):
        runtime = self.runtime
        def resume():
            sessions = runtime.service.list_sessions()
            if not sessions:
                return runtime.service.new_session()
            sid = session_id if any(row['session_id'] == session_id for row in sessions) else sessions[0]['session_id']
            response = runtime.service.get_pending_operations(sid)
            response['messages'] = runtime.service.get_history(sid)['messages']
            return response
        self.request(resume)

    def request(self, function):
        if self.busy:
            return
        self.busy = True
        self.status.set('正在处理…' if self._embedded else '正在处理… 单事项按策略保存，删除需另行确认。')
        self.send_button.configure(state='disabled')
        self.confirm_button.configure(state='disabled')
        self.cancel_button.configure(state='disabled')
        if self._embedded:
            for paint in self._action_paints:
                paint()
        self.runtime.submit(function, self.display)

    def append(self, text, role=None):
        self.transcript.configure(state='normal')
        if self._empty_transcript:
            self.transcript.delete('1.0', 'end')
            self._empty_transcript = False
        if self._embedded:
            role = role or ('user' if text.startswith('你：') else 'assistant' if text.startswith('助理：') else 'system')
            self.transcript.insert('end', text + '\n', ('preview', 'delete') if role == 'delete' else (role,))
            self.transcript.insert('end', '\n')
        else:
            self.transcript.insert('end', text+'\n\n')
        self.transcript.configure(state='disabled')
        self.transcript.see('end')

    def display(self, response):
        if not self.winfo_exists():
            return
        self.busy = False
        self.send_button.configure(state='normal')
        self.session_id = response.get('session_id') or self.session_id
        if 'sessions' in response:
            self.session_ids = [s['session_id'] for s in response['sessions']]
            labels = [s['label'] for s in response['sessions']]
            self.sessions.configure(values=labels)
            if self._embedded:
                self._session_labels = labels
            if self.session_id in self.session_ids:
                self.sessions.current(self.session_ids.index(self.session_id))
        if self._embedded:
            self._fit_session_labels()
        if response.get('error_message'):
            self.status.set('本次请求未完成；详细提示见对话区。')
            self.append('系统：'+response['error_message'])
        else:
            status = response.get('status', 'ok')
            self.status.set(self._STATUS_LABELS.get(status, self._INPUT_HINT) if self._embedded else '状态：'+status)
            if response.get('assistant_message'):
                self.append('助理：'+response['assistant_message'])
        if response.get('messages') is not None:
            self._empty_transcript = False
            self.transcript.configure(state='normal')
            self.transcript.delete('1.0', 'end')
            self.transcript.configure(state='disabled')
            for item in response['messages']:
                self.append(('你：' if item['role'] == 'user' else '助理：')+item['content'])
        self.selection_revision = response.get('selection_revision')
        self.candidates = response.get('habit_candidates') or response.get('candidates', [])
        self.candidate.configure(values=[f"{i+1}. {item['title']} · {item.get('schedule') or item.get('date', '')}" for i, item in enumerate(self.candidates)])
        self.candidate.set('')
        if self.candidates:
            types = {'general': '一般', 'urgent': '紧急', 'ddl': 'DDL'}
            for index, event in enumerate(self.candidates, 1):
                if event.get('object_type') == 'habit':
                    self.append(f"{index}. {event['title']} · {event['schedule']}")
                    continue
                status = '已完成' if event['done'] else '未完成'
                reminder = '不提醒' if event['reminder'] is None else f"提前{event['reminder']}分钟"
                self.append(f"{index}. {event['title']}\n{event['date']} 至 {event['end']} {event['time'] or '无具体时间'}\n"
                            f"{types[event['event_type']]} · {event['category'] or '无分类'} · {status} · {reminder}")
        self.operation = response.get('pending_operation')
        pending = self.operation and self.operation['status'] == 'pending'
        self.confirm_button.configure(state='normal' if pending else 'disabled')
        confirm_label = '确认执行'
        if pending and self.operation['preview'].get('after') is None and self.operation['preview'].get('before'):
            confirm_label = {
                'reviewing_batch': f"删除这{len(self.operation['preview'].get('event_ids', []))}项",
                'awaiting_final_confirmation': '最终确认删除',
            }.get(self.operation.get('stage'), '确认删除')
        self.confirm_button.configure(text=confirm_label)
        self.cancel_button.configure(state='normal' if pending else 'disabled')
        preview_start = None
        if self.operation and self.operation['status'] == 'pending':
            if self._embedded:
                preview_start = self.transcript.index('end-1c')
            self.render_operation(self.operation)
        if self._embedded:
            self.confirm_button._destructive = bool(pending and self.operation['preview'].get('after') is None
                                                     and self.operation['preview'].get('before'))
            for paint in self._action_paints:
                paint()
            (self.candidate.grid if self.candidates else self.candidate.grid_remove)()
            (self._pending_actions.grid if pending else self._pending_actions.grid_remove)()
            if preview_start is not None:
                # Put the operation heading first in a narrow transcript; the rest is scrollable.
                self.transcript.yview(preview_start)
            if not self.transcript.get('1.0', 'end-1c').strip():
                self.transcript.configure(state='normal')
                self.transcript.insert('1.0', self._EMPTY_HINT, 'empty')
                self.transcript.configure(state='disabled')
                self._empty_transcript = True
        else:
            self.input.focus_set()

    def render_operation(self, operation):
        if operation['preview'].get('object_type') == 'event_batch':
            self.append('待确认批量事项\n' + '\n'.join(
                f"{item['date']} · {item['title']} · {item['color']} · " +
                ('已完成' if item['done'] else '未完成') for item in operation['preview']['summaries']), 'preview')
            return
        if operation['preview'].get('object_type')=='batch_delete':
            rows=operation['preview']['before']
            self.append('待删除完整清单 · '+str(len(rows))+'项\n'+'\n'.join(
                f"{i}. {event_line(row)}" for i, row in enumerate(rows, 1)), 'delete')
            hint = {'reviewing_batch': '请核对后选择删除这些事项。',
                    'awaiting_final_confirmation': '下一次点击将最终删除整批事项，可一次撤销。'}
            self.append('尚未删除。'+hint.get(operation['stage'], '核对后即可确认删除，可一次撤销。'), 'delete')
            return
        if operation['preview'].get('object_type') == 'habit':
            if operation['status'] == 'pending':
                self.append('习惯创建预览：'+operation['preview']['after']['title']+' · '+operation['preview']['schedule'])
            return
        before, after = operation['preview']['before'] or {}, operation['preview']['after'] or {}
        fields = ('title', 'due', 'has_time', 'event_type', 'reminder', 'duration_days', 'skip_non_working_days', 'end_as_ddl', 'category_id', 'color', 'color_mode', 'notes', 'done')
        labels = dict(title='标题', due='开始日期 / 时间', has_time='有具体时间', event_type='事项性质', reminder='提前提醒（分钟）',
                      duration_days='持续天数', skip_non_working_days='跳过非工作日', end_as_ddl='最后一天作为DDL',
                      category_id='事项分类', color='颜色', color_mode='颜色来源', notes='备注', done='已完成')
        statuses = dict(pending='待确认（尚未写入）', succeeded='已保存', cancelled='已取消', failed='失败', expired='已过期')
        days = operation['preview']['dates']
        lines = [operation['preview']['description']+' · '+statuses.get(operation['status'], operation['status']),
                 '实际日期：'+days[0]+(' 至 '+days[-1] if len(days)>1 else '')]
        def display_value(field, value, side):
            if field == 'category_id':
                return (operation['preview'].get(side+'_summary') or {}).get('category') or '无分类'
            if value is None:
                return '无 / 不提醒' if field == 'reminder' else '无'
            if type(value) is bool:
                return '是' if value else '否'
            return {'general': '一般', 'urgent': '紧急', 'ddl': 'DDL', 'inherit': '跟随分类', 'override': '单独颜色'}.get(str(value), str(value))
        for field in fields:
            if not before and field not in {'title', 'due', 'has_time', 'event_type'} | set(operation['preview'].get('requested_fields', fields)):
                continue
            if before.get(field) != after.get(field):
                lines.append(f"{labels[field]}：{display_value(field, before.get(field), 'before')} → {display_value(field, after.get(field), 'after')}")
        self.append('\n'.join(lines), 'delete' if operation['preview'].get('after') is None else 'preview')

    def send(self, _event=None):
        value = self.input.get('1.0', 'end-1c').strip()
        if value and self.session_id and not self.busy:
            self.append('你：'+value)
            self.input.delete('1.0', 'end')
            sid, rid = self.session_id, secrets.token_urlsafe(24)
            self.request(lambda: self.runtime.service.send_message(sid, value, rid))
        return 'break'

    def select_candidate(self, _event=None):
        index = self.candidate.current()
        if index >= 0 and not self.busy:
            identifier, sid = self.candidates[index]['id'], self.session_id
            revision = self.selection_revision
            self.request(lambda: self.runtime.service.select_candidate(sid, identifier, revision=revision))

    def confirm(self):
        if self.operation and not self.busy:
            sid, oid = self.session_id, self.operation['operation_id']
            revision = self.operation['revision']
            self.request(lambda: self.runtime.service.confirm_operation(sid, oid, revision=revision))

    def cancel(self):
        if self.operation and not self.busy:
            sid, oid = self.session_id, self.operation['operation_id']
            self.request(lambda: self.runtime.service.cancel_operation(sid, oid))

    def clear(self):
        if self.session_id and not self.busy:
            sid = self.session_id
            def clear():
                self.runtime.service.clear_history(sid)
                return self.runtime.service.new_session()
            self.request(clear)

    def load_session(self, _event=None):
        # Filled on demand by the settings/history refresh action below.
        index = self.sessions.current()
        if index >= 0 and not self.busy:
            sid = self.session_ids[index]
            def load():
                response = self.runtime.service.get_history(sid)
                response.update(self.runtime.service.get_pending_operations(sid))
                response['messages'] = self.runtime.service.get_history(sid)['messages']
                return response
            self.request(load)

    def configure_ai(self):
        if not self.busy:
            AISettings(self)


class ChatDialog(_ChatWidgets, tk.Toplevel):
    def __init__(self, app, runtime):
        super().__init__(app)
        self.withdraw()
        dp = app.dpi.px
        self.title('AI 私人助理')
        width, height, x, y = dialog_geometry(app.dpi.work_area(), app.dpi.dpi,
            (app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()))
        self.geometry(f'{width}x{height}{x:+d}{y:+d}')
        self.minsize(min(width, dp(340)), min(height, dp(330)))
        self._build_chat(app, runtime, width)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.bind('<Escape>', lambda _e: self.close())
        app.present_modal(self, app)
        self.grab_set()
        self.resume()

    def close(self):
        if self.grab_current() is self:
            self.grab_release()
        self.runtime.window = None
        self.destroy()
        embedded = getattr(self.runtime, 'embedded', None)
        if embedded is not None and embedded.winfo_exists() and embedded.winfo_viewable():
            embedded.resume(embedded.session_id)


class EmbeddedChat(_ChatWidgets, tk.Frame):
    def __init__(self, parent, app, runtime, on_close, *, session_id=None, draft=''):
        super().__init__(parent)
        self._on_close = on_close
        self._build_chat(app, runtime, app.dpi.px(360), embedded=True)
        if draft:
            self.input.insert('1.0', draft)
        runtime.embedded = self
        self.resume(session_id)

    def close(self):
        self._on_close()

    def destroy(self):
        if getattr(self.runtime, 'embedded', None) is self:
            self.runtime.embedded = None
        super().destroy()


REMOTE_HELP = (
    ('首次绑定', '向服务器提供的 QQ Bot 发送“绑定 + 管理员提供的邀请码”。'
               '收到服务器地址和一次性激活码后，在本页绑定日历账号，再启用服务。'),
    ('重新绑定', '向机器人发送“重新获取激活码”，在本页填写新激活码。绑定成功后，旧设备的访问密钥会失效。'),
    ('日常使用', '向 QQ Bot 发送自然语言创建、修改、删除或查询日历事项。需要确认时，按回复继续操作。'
               '电脑关机或月历未运行时，消息会在云端等待。队列等待或处理超时可能收到提醒。'),
    ('微信兼容', '微信历史记录、原有绑定和回执继续保留。日历指令以服务器当前开放的渠道为准。'),
)
class AISettings(tk.Toplevel):
    def __init__(self, chat=None, *, app=None, runtime=None, show_remote=False):
        owner = chat.winfo_toplevel() if chat else app
        super().__init__(owner)
        self.withdraw()
        self.chat, self.app = chat, chat.app if chat else app
        self.runtime = chat.runtime if chat else runtime
        self.configuration = self.runtime.configuration
        self.old = self.configuration.load()
        theme, dp, font = self.app.theme, self.app.dpi.px, self.app.search_font
        self.title('AI 设置与隐私')
        bg = theme.panel_background
        self.configure(bg=bg)
        width, height, x, y = dialog_geometry(self.app.dpi.work_area(), self.app.dpi.dpi,
            (owner.winfo_rootx(), owner.winfo_rooty(), owner.winfo_width(), owner.winfo_height()),
            preferred=(550, 640))
        self.geometry(f'{width}x{height}{x:+d}{y:+d}')
        self.minsize(min(width, dp(320)), min(height, dp(260)))
        self.inputs = {}
        self.fields = {}
        self.status = tk.StringVar(self)
        self.binding_busy = False
        self.pending_binding = None
        self.rebinding = False
        self.history_rows = ()
        self.history_scope = self.history_key = None
        self.history_loading = False
        self.actions = actions = tk.Frame(self, bg=bg)
        actions.pack(side='bottom', fill='x', padx=dp(16), pady=dp(10))
        self.save_button, save = form_button(actions, self.app, '保存', self.save, primary=True)
        self.cancel_button, cancel = form_button(actions, self.app, '取消', self.close, quiet=True)
        save.pack(side='right')
        cancel.pack(side='right', padx=(0, dp(6)))
        status = tk.Label(self, textvariable=self.status, bg=bg, fg=theme.text_secondary,
                          font=(font, 8), anchor='w', justify='left')
        status.pack(side='bottom', fill='x', padx=dp(16))
        tk.Frame(self, height=dp(1), bg=theme.divider).pack(side='bottom', fill='x')

        style = ttk.Style(self)
        style.configure('CalendarAI.TNotebook', background=bg, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure('CalendarAI.TNotebook.Tab', font=(font, 9), padding=(dp(12), dp(6)))
        style.map('CalendarAI.TNotebook.Tab', background=[('selected', theme.accent_soft), ('!selected', bg)],
                  foreground=[('selected', theme.text_primary), ('!selected', theme.text_secondary)])
        self.tabs = ttk.Notebook(self, style='CalendarAI.TNotebook')
        self.tabs.pack(fill='both', expand=True, padx=dp(16), pady=(dp(7), 0))
        forms = []

        def add_tab(title):
            page = tk.Frame(self.tabs, bg=bg)
            self.tabs.add(page, text=title)
            scroll = SettingsScroll(page, self.app)
            scroll.pack(fill='both', expand=True)
            forms.append(scroll)
            return page, scroll

        self.ai_tab, ai_form = add_tab('远程模型')
        self.api_tab, api_form = add_tab('本机 API')
        self.remote_tab, remote_form = add_tab('远程服务')
        form, content = ai_form, ai_form.content
        wrapped = [status]

        def section(parent, title, detail):
            tk.Label(parent, text=title, bg=bg, fg=theme.text_primary,
                     font=(font, 11, 'bold'), anchor='w').pack(fill='x', pady=(dp(12), dp(2)))
            note = tk.Label(parent, text=detail, bg=bg, fg=theme.text_secondary, font=(font, 8),
                            anchor='w', justify='left')
            note.pack(fill='x', pady=(0, dp(5)))
            wrapped.append(note)

        def field(parent, key, caption, *, widget_key=None):
            tk.Label(parent, text=caption, bg=bg, fg=theme.text_primary, font=(font, 9),
                     anchor='w').pack(fill='x', pady=(0, dp(3)))
            secret = key in ('api_key', 'remote_token', 'activation_code')
            if key not in self.inputs:
                self.inputs[key] = tk.StringVar(self, '' if secret else str(self.old[key]))
            value = self.inputs[key]
            entry, control = form_entry(parent, self.app, value, show='*' if secret else '')
            self.fields[widget_key or key] = entry
            control.pack(fill='x', pady=(0, dp(7)), ipady=0 if theme.rounded_widget_shells else dp(4))
            entry.bind('<FocusIn>', lambda _e, scroll=form: scroll.reveal(control), add='+')

        def option(parent, text, variable):
            check = themed_check(parent, theme, dp, text=text, variable=variable,
                                 bg=bg, fg=theme.text_primary, selectcolor=theme.input_background,
                                 activebackground=bg, activeforeground=theme.text_primary,
                                 font=(font, 9), anchor='w', justify='left', cursor='hand2')
            check.pack(fill='x', pady=(0, dp(6)))
            check.bind('<FocusIn>', lambda _e, scroll=form: scroll.reveal(check), add='+')
            wrapped.append(check)
            return check

        self.consent = tk.BooleanVar(value=self.old['consent'])
        self.http = tk.BooleanVar(value=self.old['http_enabled'])
        section(content, '远程模型', '仅发送必要的消息及查询到的部分日历信息，不会上传完整日历或备份。')
        field(content, 'endpoint', '服务地址（HTTPS）')
        row = tk.Frame(content, bg=bg)
        row.pack(fill='x')
        model = tk.Frame(row, bg=bg)
        timeout = tk.Frame(row, bg=bg)
        timeout.pack(side='right')
        model.pack(side='left', fill='x', expand=True, padx=(0, dp(8)))
        field(model, 'model', '模型名称')
        field(timeout, 'timeout', '超时（5–60 秒）')
        field(content, 'api_key', 'API Key（留空保留已有密钥）')
        option(content, '我同意将上述信息发给已配置的远程模型', self.consent)

        form, content = api_form, api_form.content
        section(content, '本机 API', '仅监听 127.0.0.1，连接必须使用 Bearer Token。')
        option(content, '启用本机 HTTP API', self.http)
        local = tk.Frame(content, bg=bg)
        local.pack(fill='x', pady=(0, dp(6)))
        port = tk.Frame(local, bg=bg)
        port.pack(side='left')
        field(port, 'port', '端口')
        self.copy_button, copy = form_button(local, self.app, '复制本机 Token', self.copy_token)
        copy.pack(side='left', anchor='s', padx=(dp(10), 0), pady=(0, dp(7)))
        self.copy_button.bind('<FocusIn>', lambda _e, scroll=form: scroll.reveal(copy), add='+')
        self.clear_ai_button, clear_ai = form_button(content, self.app, '清除 AI 助理设置', self.clear_ai_settings, quiet=True)
        clear_ai.pack(anchor='w', pady=(0, dp(4)))
        self.clear_ai_button.bind('<FocusIn>', lambda _e: api_form.reveal(clear_ai), add='+')

        form, content = remote_form, remote_form.content
        self.remote_enabled = tk.BooleanVar(value=self.old['remote_enabled'])
        self.remote_enable_check = option(content, '启用私人远程服务', self.remote_enabled)
        self.remote_status = tk.StringVar(self)
        self.connection_detail = tk.StringVar(self)
        self.binding_state = tk.StringVar(self)
        self.account_name = tk.StringVar(self)
        self.ai_state = tk.StringVar(self)
        self.last_check_time = tk.StringVar(self)
        self.check_result = tk.StringVar(self)
        self.latest_result = tk.StringVar(self)
        status_bg = theme.input_background
        status_card = (RoundedCardShell(content, theme, dp, status_bg, surface=bg, outline=theme.input_border,
                                        cursor='arrow') if theme.rounded_widget_shells else
                       tk.Frame(content, bg=status_bg, highlightthickness=dp(1), highlightbackground=theme.input_border))
        status_card.pack(fill='x', pady=(dp(3), dp(8)))
        details = tk.Frame(status_card, bg=status_bg)
        details.pack(fill='x', padx=dp(11), pady=dp(8))
        self.connection_label = tk.Label(details, textvariable=self.remote_status, bg=status_bg,
                                         fg=theme.text_primary, font=(font, 12, 'bold'), anchor='w')
        self.connection_label.pack(fill='x')
        detail = tk.Label(details, textvariable=self.connection_detail, bg=status_bg,
                          fg=theme.text_secondary, font=(font, 8), anchor='w', justify='left')
        detail.pack(fill='x', pady=(dp(2), dp(6)))
        wrapped.append(detail)
        for label, variable in (('绑定状态', self.binding_state), ('日历账号', self.account_name), ('AI 助理', self.ai_state),
                                ('最近成功检查', self.last_check_time), ('检查结果', self.check_result),
                                ('最近处理', self.latest_result)):
            row = tk.Frame(details, bg=status_bg)
            row.pack(fill='x', pady=dp(1))
            tk.Label(row, text=label, bg=status_bg, fg=theme.text_secondary, font=(font, 9)).pack(side='left')
            tk.Label(row, textvariable=variable, bg=status_bg, fg=theme.text_primary, font=(font, 9),
                     wraplength=dp(230), justify='right').pack(side='right')
        actions = tk.Frame(details, bg=status_bg)
        actions.pack(fill='x', pady=(dp(6), 0))
        self.manual_check_button, manual_check = form_button(actions, self.app, '手动检查', self.check_remote_jobs)
        manual_check.pack(side='left', padx=(0, dp(6)))
        self.manual_check_button.bind('<FocusIn>', lambda _e: form.reveal(manual_check), add='+')
        self.rebind_button, rebind = form_button(actions, self.app, '重新绑定', self.start_rebinding)
        rebind.pack(side='left')
        self.rebind_button.bind('<FocusIn>', lambda _e: form.reveal(rebind), add='+')
        self.copy_rebinding_button, self.copy_rebinding_control = form_button(
            details, self.app, '复制「重新获取激活码」', self.copy_rebinding_command, quiet=True)

        self.remote_mode = tk.StringVar(self, 'auto' if self.old['remote_account'] or not self.old['remote_token'] else 'manual')
        self.auto_remote = tk.Frame(content, bg=bg)
        field(self.auto_remote, 'remote_url', '服务器地址（HTTPS 根地址）')
        field(self.auto_remote, 'activation_code', '客户端激活码')
        self.bind_button, bind = form_button(self.auto_remote, self.app, '绑定', self.bind_remote)
        bind.pack(anchor='w', pady=(0, dp(4)))
        self.bind_button.bind('<FocusIn>', lambda _e: form.reveal(bind), add='+')
        self.remote_mode_anchor = tk.Frame(content, bg=bg)
        self.remote_mode_anchor.pack(fill='x')

        history = self.add_fold(content, '最近远程处理', form, expanded=True)
        history_row = tk.Frame(history, bg=bg)
        history_row.pack(fill='x', pady=(0, dp(3)))
        self.history_view = tk.Text(history_row, height=9, width=1, wrap='word', state='disabled',
                                    bg=theme.input_background, fg=theme.text_primary, font=(font, 9),
                                    padx=dp(8), pady=dp(5), bd=0, highlightthickness=dp(1),
                                    highlightbackground=theme.input_border)
        bar = ttk.Scrollbar(history_row, orient='vertical', command=self.history_view.yview)
        bar.pack(side='right', fill='y')
        self.history_view.configure(yscrollcommand=bar.set)
        self.history_view.pack(side='left', expand=True, fill='x')
        self.history_view.tag_configure('secondary', foreground=theme.text_secondary, spacing3=dp(3))
        self.history_view.bind('<MouseWheel>', self.scroll_history)
        self.paint_history()
        tk.Label(history, text='最近 20 条本机记录', bg=bg, fg=theme.text_secondary,
                 font=(font, 8), anchor='w').pack(fill='x', pady=(0, dp(5)))
        help_body = self.add_fold(content, '绑定与使用说明', form)
        for title, description in REMOTE_HELP:
            section(help_body, title, description)

        advanced = self.add_fold(content, '高级连接设置', form)
        field(advanced, 'remote_url', '服务器地址（HTTPS 根地址）', widget_key='advanced_url')
        modes = tk.Frame(advanced, bg=bg)
        modes.pack(fill='x', pady=(0, dp(4)))
        self.remote_mode_controls = []
        for mode, label in (('auto', '自动绑定（推荐）'), ('manual', '手动配置')):
            control = tk.Radiobutton(modes, text=label, value=mode, variable=self.remote_mode,
                                     command=self.show_remote_mode, bg=bg, fg=theme.text_primary,
                                     activebackground=bg, activeforeground=theme.text_primary,
                                     selectcolor=theme.input_background, font=(font, 9))
            control.pack(side='left')
            control.bind('<FocusIn>', lambda _e: form.reveal(modes), add='+')
            self.remote_mode_controls.append(control)
        self.manual_remote = tk.Frame(advanced, bg=bg)
        field(self.manual_remote, 'remote_token', 'Worker Token（同一地址留空保留已有密钥）')
        safety_row = self.safety_row = tk.Frame(advanced, bg=bg)
        safety_row.pack(fill='x', pady=(0, dp(6)))
        tk.Label(safety_row, text='在线兜底检查', bg=bg, fg=theme.text_primary,
                 font=(font, 9)).pack(side='left')
        self.safety_check = ttk.Combobox(safety_row, state='readonly', font=(font, 9), width=10,
                                         values=tuple(f'{minutes} 分钟' if minutes else '关闭'
                                                      for minutes in SAFETY_CHECK_MINUTES))
        minutes = self.old['remote_safety_check_minutes']
        self.safety_check.current(SAFETY_CHECK_MINUTES.index(minutes) if minutes in SAFETY_CHECK_MINUTES else 0)
        self.safety_check.pack(side='right')
        self.safety_check.bind('<FocusIn>', lambda _e: form.reveal(safety_row), add='+')
        remote_actions = tk.Frame(advanced, bg=bg)
        remote_actions.pack(fill='x', pady=(0, dp(4)))
        self.test_remote_button, test = form_button(remote_actions, self.app, '测试连接', self.check_remote_connection)
        test.pack(side='left')
        self.test_remote_button.bind('<FocusIn>', lambda _e: form.reveal(test), add='+')
        self.clear_remote_button, clear_remote = form_button(remote_actions, self.app, '清除绑定信息',
                                                             self.clear_remote_binding, quiet=True)
        clear_remote.pack(side='left', padx=(dp(6), 0))
        self.clear_remote_button.bind('<FocusIn>', lambda _e: form.reveal(clear_remote), add='+')
        self.show_remote_mode()
        self.tabs.bind('<<NotebookTabChanged>>', self.refresh_remote_panel, add='+')
        if show_remote:
            self.tabs.select(self.remote_tab)
        self.watch_remote_status()
        for key in ('timeout', 'port'):
            self.fields[key].configure(width=11)
        def wrap(event):
            for widget in wrapped:
                widget.configure(wraplength=max(dp(80), event.width - dp(20)))
        for scroll in forms:
            scroll.canvas.bind('<Configure>', wrap, add='+')
        for widget in wrapped:
            widget.configure(wraplength=max(dp(80), width-dp(60)))
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.bind('<Escape>', lambda _e: self.close())
        self.app.present_modal(self, owner)
        self.grab_set()

    def copy_token(self):
        token = self.configuration.load().get('token')
        if token:
            self.clipboard_clear()
            self.clipboard_append(token)
            self.status.set('已复制。仅交给可信本机客户端；不要粘贴到聊天或公开日志。')
        else:
            self.status.set('请先保存设置以生成Token。')

    def watch_remote_status(self):
        self.refresh_remote_panel()
        self.remote_status_poll = self.after(1000, self.watch_remote_status)

    def refresh_remote_panel(self, _event=None):
        if self.tabs.select() == str(self.remote_tab):
            worker = self.runtime.core.remote
            snapshot = worker.snapshot() if worker else RemoteStatus('unavailable' if self.old['remote_enabled'] else 'disabled')
            if snapshot.account_verified and not self.old['remote_account']:
                config = self.configuration.load()
                if all(config[key] == self.old[key] for key in ('remote_url', 'remote_token')):
                    self.old['remote_account'] = config['remote_account']
            self.refresh_history(snapshot)
            self.render_remote_status(snapshot)

    def show_remote_mode(self):
        self.auto_remote.pack_forget()
        self.manual_remote.pack_forget()
        if self.remote_mode.get() == 'manual':
            self.manual_remote.pack(fill='x', before=self.safety_row)
        elif self.rebinding or not self.old['remote_token'] or self.pending_binding:
            self.auto_remote.pack(fill='x', before=self.remote_mode_anchor)

    def add_fold(self, parent, title, scroll, *, expanded=False):
        section = tk.Frame(parent, bg=parent.cget('bg'))
        section.pack(fill='x', pady=(0, self.app.dpi.px(5)))
        body = tk.Frame(section, bg=section.cget('bg'))

        def toggle():
            nonlocal expanded
            expanded = not expanded
            button.configure(text=('▾  ' if expanded else '▸  ') + title)
            if expanded:
                body.pack(fill='x')
            else:
                body.pack_forget()

        button, control = form_button(section, self.app, ('▾  ' if expanded else '▸  ') + title, toggle, quiet=True)
        control.pack(anchor='w')
        button.bind('<FocusIn>', lambda _e: scroll.reveal(control), add='+')
        if expanded:
            body.pack(fill='x')
        if title == '高级连接设置':
            self.advanced_toggle = button
        return body

    def start_rebinding(self):
        if self.binding_busy or self.pending_binding:
            return
        self.rebinding = True
        self.remote_mode.set('auto')
        if self.old['remote_url']:
            self.inputs['remote_url'].set(self.old['remote_url'])
        self.inputs['activation_code'].set('')
        self.show_remote_mode()
        self.fields['activation_code'].focus_set()

    def copy_rebinding_command(self):
        self.clipboard_clear()
        self.clipboard_append('重新获取激活码')
        self.status.set('已复制。请发送给服务器提供的机器人。')

    def render_remote_status(self, snapshot):
        state = snapshot.connection
        invalid = state in ('invalid', 'mismatch')
        available = not invalid and state not in ('disabled', 'verifying')
        busy = self.binding_busy or self.pending_binding is not None
        can_check = available and not busy and self.runtime.core.remote and snapshot.check_result != 'checking'
        self.manual_check_button.configure(state='normal' if can_check else 'disabled')
        self.rebind_button.configure(text='重新绑定' if self.old['remote_token'] else '开始绑定',
                                     state='disabled' if busy else 'normal')
        if invalid:
            if not self.copy_rebinding_control.winfo_manager():
                self.copy_rebinding_control.pack(anchor='w')
        else:
            self.copy_rebinding_control.pack_forget()
        title, detail = CONNECTION_LABELS[state]
        self.remote_status.set(title)
        theme = self.app.theme
        self.connection_label.configure(fg=theme.quick_success if state in ('live', 'backup') else
                                        theme.event_type_urgent if invalid else theme.text_primary)
        if snapshot.processing_id:
            detail += ' 正在处理远程消息。'
        if state == 'live' and snapshot.request_failed:
            detail += ' 任务接口暂不可用，正在重试。'
        self.connection_detail.set(detail)
        self.binding_state.set('账号不符' if state == 'mismatch' else '已失效' if invalid else '已绑定到本机' if self.old['remote_account'] else
                               '已配置手动连接' if self.old['remote_token'] else '尚未绑定')
        self.account_name.set(snapshot.account_name or self.old['remote_display_name'] or '—')
        checked_at = snapshot.last_checked_at
        self.last_check_time.set(datetime.fromtimestamp(checked_at).strftime('%m-%d %H:%M:%S')
                                 if checked_at is not None else '暂无')
        self.check_result.set({'checking': '正在检查……', 'empty': '无事发生，一切正常',
                               'claimed': '已领取远程任务', 'failed': '本次检查失败'}.get(snapshot.check_result, '暂无'))
        finished = (row for row in self.history_rows if row.result not in ('正在处理', '已接收，等待处理'))
        latest = max(finished, key=lambda row: row.processed_at or row.received_at, default=None)
        recent = snapshot.completion
        if latest and (not recent or latest.processed_at >= recent.processed_at):
            recent = latest
        self.ai_state.set(model_status(bool(self.old['api_key']), self.old['consent'],
                                       recent.model_unavailable if recent else False))
        self.latest_result.set(f'{self.history_time(latest.processed_at)} · {latest.result}' if latest and latest.processed_at
                               else latest.result if latest else '暂无')

    @staticmethod
    def history_time(timestamp):
        return datetime.fromtimestamp(timestamp).strftime('%m-%d %H:%M')

    def refresh_history(self, snapshot):
        scope = tuple(self.old[key] for key in ('remote_url', 'remote_token', 'remote_account', 'remote_legacy_credential'))
        if self.history_scope != scope:
            self.history_scope = scope
            self.history_rows = ()
            self.history_key = None
            self.paint_history()
        key = (scope, snapshot.history_revision, snapshot.processing_id)
        if self.history_loading or key == self.history_key:
            return
        self.history_key, self.history_loading = key, True
        config = {**self.old}

        def finish(rows):
            if not self.winfo_exists():
                return
            self.history_loading = False
            if self.history_key == key and self.history_scope == scope:
                self.history_rows = rows or ()
                self.paint_history(unavailable=rows is None)

        def run():
            try:
                rows = self.runtime.core.recent_remote_history(config, snapshot.processing_id)
            except (OSError, ValueError, sqlite3.Error):
                rows = None
            self.app._call_main(lambda: finish(rows))

        threading.Thread(target=run, daemon=True, name='calendar-remote-history').start()

    def paint_history(self, unavailable=False):
        view = self.history_view
        top = view.yview()[0]
        view.configure(state='normal')
        view.delete('1.0', 'end')
        if not self.history_rows:
            view.insert('end', '本机记录暂不可读取。' if unavailable else '暂无本机处理记录。', 'secondary')
        for row in self.history_rows:
            view.insert('end', f'{self.history_time(row.received_at)}  ·  {channel_name(row.channel)}  ·  {row.result}\n')
            delivery = ' · '.join(filter(None, (row.upload, row.notification)))
            view.insert('end', f'{delivery}\n', 'secondary')
        view.configure(state='disabled')
        view.yview_moveto(top)

    def scroll_history(self, event):
        self.history_view.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        return 'break'

    def bind_remote(self):
        if self.binding_busy:
            return
        core = self.runtime.core
        if self.pending_binding:
            url, binding = self.pending_binding
            code = None
        else:
            try:
                url = normalize_url(self.inputs['remote_url'].get().strip())
                code = self.inputs['activation_code'].get().strip()
                if not code:
                    raise ValueError('请填写客户端激活码。')
            except ValueError as error:
                self.status.set(str(error))
                return
            binding = None
        self.binding_busy = True
        self.update_binding_controls()
        self.status.set('正在保存绑定…' if binding else '正在绑定…')

        def finish(config, pending, message):
            if not self.winfo_exists():
                return
            self.binding_busy = False
            self.pending_binding = pending
            self.update_binding_controls()
            self.status.set(message)
            if config is None:
                return
            self.old = config
            self.rebinding = False
            self.remote_enabled.set(False)
            self.inputs['activation_code'].set('')
            self.inputs['remote_token'].set('')
            self.inputs['remote_url'].set(config['remote_url'])
            self.show_remote_mode()
            self.refresh_remote_panel()

        def run():
            config, pending = None, None
            try:
                activated = binding or core.request_remote_binding(url, code)
            except ValueError as error:
                message = str(error)
            except (Unavailable, StatusError, OSError):
                message = '绑定未收到有效响应，请核对绑定状态。'
            else:
                try:
                    config = core.save_remote_binding(url, activated)
                except (OSError, ValueError):
                    pending = (url, activated)
                    message = '服务器已完成激活。本机保存失败，请点击“重试本地保存”。'
                else:
                    message = '已保存绑定。启用远程服务后即可接收任务。'
            self.app._call_main(lambda: finish(config, pending, message))

        # Let an in-flight activation persist its Token even during normal application shutdown.
        threading.Thread(target=run, daemon=False, name='calendar-wechat-bind').start()

    def update_binding_controls(self):
        locked = self.binding_busy or self.pending_binding is not None
        state = 'disabled' if locked else 'normal'
        for control in (self.save_button, self.remote_enable_check, self.fields['remote_url'], self.fields['advanced_url'],
                        self.fields['activation_code'], *self.remote_mode_controls):
            control.configure(state=state)
        self.bind_button.configure(state='disabled' if self.binding_busy else 'normal',
                                   text='重试本地保存' if self.pending_binding else '绑定')
        self.cancel_button.configure(state='disabled' if self.binding_busy else 'normal')
        self.clear_remote_button.configure(state='disabled' if self.binding_busy else 'normal')
        self.clear_ai_button.configure(state=state)

    def clear_remote_binding(self):
        if self.binding_busy:
            return
        if not messagebox.askyesno('清除绑定信息', '清除本机的远程服务器地址、账户和 Worker Token，并停止接单？',
                                   parent=self, default='no'):
            return
        try:
            self.old = self.runtime.core.clear_remote_binding()
        except (OSError, ValueError):
            self.status.set('无法清除本机绑定信息，请检查配置目录。')
            return
        self.pending_binding = None
        self.rebinding = False
        self.update_binding_controls()
        self.remote_enabled.set(False)
        for key in ('remote_url', 'remote_token', 'activation_code'):
            self.inputs[key].set('')
        self.remote_mode.set('auto')
        self.show_remote_mode()
        self.refresh_remote_panel()
        self.status.set('本机绑定已清除，远程服务已关闭。')

    def clear_ai_settings(self):
        if self.binding_busy or self.pending_binding:
            return
        if not messagebox.askyesno('清除 AI 助理设置', '清除模型密钥和授权，重置模型与本机 API 设置，并暂停远程接单？',
                                   parent=self, default='no'):
            return
        try:
            self.old = self.runtime.clear_ai_settings()
        except (OSError, ValueError):
            self.status.set('无法重置 AI 助理设置，请检查配置目录。')
            return
        for key in ('endpoint', 'model', 'timeout', 'port'):
            self.inputs[key].set(str(self.old[key]))
        self.inputs['api_key'].set('')
        self.consent.set(False)
        self.http.set(False)
        self.remote_enabled.set(False)
        self.refresh_remote_panel()
        self.status.set('AI 助理设置已重置，远程接单已暂停。')

    def check_remote_connection(self):
        try:
            url = normalize_url(self.inputs['remote_url'].get().strip())
        except ValueError as error:
            self.status.set(str(error))
            return
        self.test_remote_button.configure(state='disabled')
        self.status.set('正在检查公共健康接口…')

        def finish(message):
            if self.winfo_exists():
                self.status.set(message)
                self.test_remote_button.configure(state='normal')

        def run():
            try:
                healthy = test_connection(url)
            except (StatusError, Unavailable):
                healthy = False
            message = '公共健康检查通过。' if healthy else '公共健康检查失败。'
            self.app._call_main(lambda: finish(message + ' 本次检查未验证 Worker 身份。'))

        threading.Thread(target=run, daemon=True, name='calendar-wechat-health').start()

    def check_remote_jobs(self):
        if self.binding_busy or self.pending_binding:
            return
        worker = self.runtime.core.remote
        if self.old['remote_enabled'] and worker and worker.request_check():
            self.refresh_remote_panel()
        else:
            self.status.set('请先启用远程服务并保存。')

    def save(self):
        if self.binding_busy or self.pending_binding:
            return
        try:
            config = {**self.old, **{key: value.get().strip() for key, value in self.inputs.items() if key != 'activation_code'},
                      'consent': self.consent.get(), 'http_enabled': self.http.get(),
                      'remote_enabled': self.remote_enabled.get(),
                      'remote_safety_check_minutes': SAFETY_CHECK_MINUTES[max(0, self.safety_check.current())]}
            config['api_key'] = config['api_key'] or self.old['api_key']
            if config['remote_url']:
                config['remote_url'] = normalize_url(config['remote_url'])
            same_url = config['remote_url'] == self.old['remote_url']
            if self.remote_mode.get() == 'auto':
                bound = same_url and self.old['remote_token']
                if config['remote_enabled'] and not bound:
                    raise ValueError('请先绑定当前服务器，或切换到手动配置。')
                config['remote_token'] = self.old['remote_token'] if same_url else ''
            elif same_url and not config['remote_token']:
                config['remote_token'] = self.old['remote_token']
            if not same_url or config['remote_token'] != self.old['remote_token']:
                config['remote_account'] = ''
                config['remote_display_name'] = ''
            config['timeout'], config['port'] = int(config['timeout']), int(config['port'])
            self.runtime.core.save_settings(config)
            self.runtime.apply_http()
        except (OSError, ValueError) as error:
            self.status.set(str(error))
            return
        if self.chat:
            self.chat.status.set('设置已保存。')
        self.close()

    def close(self):
        if self.binding_busy:
            return
        self.after_cancel(self.remote_status_poll)
        if self.grab_current() is self:
            self.grab_release()
        self.destroy()
        if self.chat and self.chat.winfo_exists():
            if not self.chat._embedded:
                self.chat.grab_set()
            self.chat.input.focus_set()

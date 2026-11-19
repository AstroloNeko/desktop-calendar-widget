"""Desktop adapter. Remote work/service locks never block Tk's main thread."""
from pathlib import Path
import secrets
import threading
import tkinter as tk
from tkinter import font as tkfont, ttk

from tk_dispatcher import TkDispatcher
from desktop_calendar.assistant.calendar_chat_protocol import ChatError, envelope
from desktop_calendar.assistant.llm_provider import AIConfiguration
from desktop_calendar.reminders.reminder_ui import reminder_button
from desktop_calendar.ui.themed_controls import (RoundedCardShell, SettingsScroll, form_button,
                                                  form_entry, themed_check)
from desktop_calendar.updates.update_dialog import dialog_geometry
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
            writable=lambda: not app.lifecycle.exiting and not app.update_busy and not app._lifecycle_operation_depth
            and (app._active_modal_window() is None or app._active_modal_window() is self.window),
            habit_refresh=self.refresh_habits)
        self.journal, self.gateway, self.service = self.core.journal, self.core.gateway, self.core.service
        self.jobs = threading.BoundedSemaphore(4)
        self.error = None
        try:
            self.apply_http()
        except ChatError as error:
            self.error = str(error)

    def refresh_habits(self):
        self.app.invalidate_reminders()
        self.app.render()
        manager = self.app.routine_manager
        if manager and manager.winfo_exists():
            manager.refresh()

    def apply_http(self):
        try:
            self.core.apply_http()
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


def start_chat_if_enabled(app):
    """Only explicitly enabled HTTP resumes at startup; no model request or implicit opt-in."""
    if getattr(app, 'chat_runtime', None) is not None or app.lifecycle.exiting or app.lifecycle.stopped:
        return  # A user may open AI before the delayed startup callback fires.
    folder = app.store.data_file.parent / 'ai-assistant'
    if not (folder / 'configuration.dpapi').exists():
        return
    try:
        if AIConfiguration(folder).load().get('http_enabled'):
            app.chat_runtime = ChatRuntime(app)
            if app.chat_runtime.error:
                app._show_feedback(app.chat_runtime.error)
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
        self.confirm_button.configure(text=('删除这'+str(len(self.operation['preview']['event_ids']))+'项' if self.operation.get('stage')=='reviewing_batch' else '最终确认删除')
            if pending and self.operation['preview'].get('object_type')=='batch_delete' else '确认执行')
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
                r['date']+' '+(r['time'] or '')+' · '+r['title']+' · ID '+r['id'] for r in rows), 'delete')
            self.append('尚未删除。'+('请核对后选择删除这些事项。' if operation['stage']=='reviewing_batch' else '下一次点击将最终删除整批事项，可一次撤销。'), 'delete')
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


class AISettings(tk.Toplevel):
    def __init__(self, chat):
        owner = chat.winfo_toplevel()
        super().__init__(owner)
        self.withdraw()
        self.chat, self.app = chat, chat.app
        self.configuration = chat.runtime.configuration
        self.old = self.configuration.load()
        theme, dp, font = self.app.theme, self.app.dpi.px, self.app.search_font
        self.title('AI 设置与隐私')
        bg = theme.panel_background
        self.configure(bg=bg)
        width, height, x, y = dialog_geometry(self.app.dpi.work_area(), self.app.dpi.dpi,
            (owner.winfo_rootx(), owner.winfo_rooty(), owner.winfo_width(), owner.winfo_height()),
            preferred=(480, 485))
        self.geometry(f'{width}x{height}{x:+d}{y:+d}')
        self.minsize(min(width, dp(320)), min(height, dp(260)))
        self.inputs = {}
        self.fields = {}
        self.status = tk.StringVar(self)
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

        self.form = form = SettingsScroll(self, self.app)
        form.pack(fill='both', expand=True, padx=dp(16))
        content = form.content
        wrapped = [status]

        def section(parent, title, detail):
            tk.Label(parent, text=title, bg=bg, fg=theme.text_primary,
                     font=(font, 11, 'bold'), anchor='w').pack(fill='x', pady=(dp(12), dp(2)))
            note = tk.Label(parent, text=detail, bg=bg, fg=theme.text_secondary, font=(font, 8),
                            anchor='w', justify='left')
            note.pack(fill='x', pady=(0, dp(5)))
            wrapped.append(note)

        def field(parent, key, caption):
            tk.Label(parent, text=caption, bg=bg, fg=theme.text_primary, font=(font, 9),
                     anchor='w').pack(fill='x', pady=(0, dp(3)))
            value = tk.StringVar(value='' if key == 'api_key' else str(self.old[key]))
            self.inputs[key] = value
            entry, control = form_entry(parent, self.app, value, show='*' if key == 'api_key' else '')
            self.fields[key] = entry
            control.pack(fill='x', pady=(0, dp(7)), ipady=0 if theme.rounded_widget_shells else dp(4))
            entry.bind('<FocusIn>', lambda _e: form.reveal(control), add='+')

        def option(parent, text, variable):
            check = themed_check(parent, theme, dp, text=text, variable=variable,
                                 bg=bg, fg=theme.text_primary, selectcolor=theme.input_background,
                                 activebackground=bg, activeforeground=theme.text_primary,
                                 font=(font, 9), anchor='w', justify='left', cursor='hand2')
            check.pack(fill='x', pady=(0, dp(6)))
            check.bind('<FocusIn>', lambda _e: form.reveal(check), add='+')
            wrapped.append(check)

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

        tk.Frame(content, height=dp(1), bg=theme.divider).pack(fill='x', pady=(dp(4), 0))
        section(content, '本机 API', '仅监听 127.0.0.1，连接必须使用 Bearer Token。')
        option(content, '启用本机 HTTP API', self.http)
        local = tk.Frame(content, bg=bg)
        local.pack(fill='x', pady=(0, dp(6)))
        port = tk.Frame(local, bg=bg)
        port.pack(side='left')
        field(port, 'port', '端口')
        self.copy_button, copy = form_button(local, self.app, '复制本机 Token', self.copy_token)
        copy.pack(side='left', anchor='s', padx=(dp(10), 0), pady=(0, dp(7)))
        self.copy_button.bind('<FocusIn>', lambda _e: form.reveal(copy), add='+')
        for key in ('timeout', 'port'):
            self.fields[key].configure(width=11)
        def wrap(event):
            for widget in wrapped:
                widget.configure(wraplength=max(dp(80), event.width - dp(20)))
        form.canvas.bind('<Configure>', wrap, add='+')
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

    def save(self):
        try:
            config = {**self.old, **{key: value.get().strip() for key, value in self.inputs.items()},
                      'consent': self.consent.get(), 'http_enabled': self.http.get()}
            config['api_key'] = config['api_key'] or self.old['api_key']
            config['timeout'], config['port'] = int(config['timeout']), int(config['port'])
            self.configuration.save(config)
            self.chat.runtime.apply_http()
        except (OSError, ValueError) as error:
            self.status.set(str(error))
            return
        self.chat.status.set('设置已保存；必要的聊天及查询数据会发送给已配置模型。')
        self.close()

    def close(self):
        if self.grab_current() is self:
            self.grab_release()
        self.destroy()
        if self.chat.winfo_exists():
            if not self.chat._embedded:
                self.chat.grab_set()
            self.chat.input.focus_set()

"""Desktop adapter. Remote work/service locks never block Tk's main thread."""
from pathlib import Path
import secrets
import threading
import tkinter as tk
from tkinter import ttk

from calendar_action_gateway import CalendarActionGateway, TkDispatcher
from calendar_chat_http import LocalChatHTTP
from calendar_chat_service import CalendarChatService, envelope
from calendar_chat_sessions import ChatJournal
from calendar_chat_tools import ChatError
from llm_provider import AIConfiguration, DeepSeekProvider
from reminder_ui import reminder_button
from themed_controls import themed_check
from update_dialog import dialog_geometry


class ChatRuntime:
    def __init__(self, app):
        self.app, self.window, self.http = app, None, None
        self.folder = app.store.data_file.parent / 'ai-assistant'
        self.configuration = AIConfiguration(self.folder)
        self.journal = ChatJournal(self.folder / 'sessions.sqlite3', self.configuration.protector)
        self.gateway = CalendarActionGateway(app.store, app.event_commands, TkDispatcher(app), app._refresh_event_change,
            lambda: not app.lifecycle.exiting and not app.update_busy and not app._lifecycle_operation_depth
            and (app._active_modal_window() is None or app._active_modal_window() is self.window), self.refresh_habits)
        self.service = CalendarChatService(self.gateway, self.journal, DeepSeekProvider(self.configuration))
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
        config = self.configuration.load()
        if self.http:
            self.http.stop()
            self.http = None
        if config.get('http_enabled'):
            server = LocalChatHTTP(self.service, config['token'], config['port'])
            try:
                server.start()
            except OSError:
                raise ChatError('service_unavailable', '本机API端口不可用；请更换端口或关闭API。桌面聊天仍可使用。') from None
            self.http = server

    def submit(self, function, callback):
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
            self.app._call_main(lambda: callback(result))
        threading.Thread(target=run, daemon=True, name='calendar-chat-job').start()

    def stop(self):
        self.service.stopped = True
        if self.http:
            self.http.stop()
            self.http = None


def open_chat(app):
    blocker = app._active_modal_window()
    runtime = getattr(app, 'chat_runtime', None)
    if blocker:
        app._focus_lifecycle_owner(blocker)
        return
    if runtime is None:
        try:
            runtime = ChatRuntime(app)
            app.chat_runtime = runtime
        except (OSError, ValueError):
            app._show_feedback('AI组件无法初始化；日历仍可正常使用，请检查本机配置目录。')
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


class ChatDialog(tk.Toplevel):
    def __init__(self, app, runtime):
        super().__init__(app)
        self.withdraw()
        self.app, self.runtime, self.session_id, self.operation, self.busy = app, runtime, None, None, False
        theme, dp, font = app.theme, app.dpi.px, app.search_font
        self.title('AI 私人助理')
        self.configure(bg=theme.panel_background)
        width, height, x, y = dialog_geometry(app.dpi.work_area(), app.dpi.dpi,
            (app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()))
        self.geometry(f'{width}x{height}{x:+d}{y:+d}')
        self.minsize(min(width, dp(340)), min(height, dp(330)))
        bar = tk.Frame(self, bg=theme.panel_background)
        bar.pack(fill='x', padx=dp(12), pady=dp(6))
        reminder_button(bar, app, '模型 / 本机API设置', self.configure_ai).pack(side='right')
        reminder_button(bar, app, '新对话', lambda: self.request(self.runtime.service.new_session)).pack(side='left')
        reminder_button(bar, app, '清除此对话', self.clear).pack(side='left', padx=dp(5))
        self.sessions = ttk.Combobox(bar, state='readonly', width=10)
        self.sessions.pack(side='left')
        self.sessions.bind('<<ComboboxSelected>>', self.load_session)
        bottom = tk.Frame(self, bg=theme.panel_background)
        bottom.pack(side='bottom', fill='x', padx=dp(12), pady=dp(8))
        self.status = tk.StringVar(value='未启用远程模型。请先阅读隐私说明并配置自己的密钥。')
        tk.Label(bottom, textvariable=self.status, font=(font, 8), wraplength=width-dp(40), justify='left',
                 bg=theme.panel_background, fg=theme.text_secondary).pack(fill='x')
        self.input = tk.Text(bottom, height=3, wrap='word', font=(font, 10), bg=theme.input_background,
                             fg=theme.text_primary, insertbackground=theme.text_primary, undo=True)
        self.input.pack(fill='x', pady=dp(5))
        self.input.bind('<Return>', self.send)
        self.input.bind('<Shift-Return>', lambda _e: None)
        self.send_button = reminder_button(bottom, app, '发送', self.send, primary=True)
        self.send_button.pack(side='right')
        reminder_button(bottom, app, '关闭', self.close).pack(side='right', padx=dp(5))
        self.confirm_button = reminder_button(bottom, app, '确认此操作', self.confirm)
        self.confirm_button.pack(side='left')
        self.cancel_button = reminder_button(bottom, app, '取消操作', self.cancel)
        self.cancel_button.pack(side='left', padx=dp(5))
        self.confirm_button.configure(state='disabled')
        self.cancel_button.configure(state='disabled')
        self.candidate = ttk.Combobox(bottom, state='readonly')
        self.candidate.pack(fill='x', pady=dp(4))
        self.candidate.bind('<<ComboboxSelected>>', self.select_candidate)
        frame = tk.Frame(self, bg=theme.panel_background)
        frame.pack(fill='both', expand=True, padx=dp(12))
        self.transcript = tk.Text(frame, wrap='word', font=(font, 10), bg=theme.workspace_detail, fg=theme.text_primary,
                                  relief='flat', state='disabled', padx=dp(8), pady=dp(6))
        scroll = ttk.Scrollbar(frame, command=self.transcript.yview, style='Global.Vertical.TScrollbar')
        scroll.pack(side='right', fill='y')
        self.transcript.configure(yscrollcommand=scroll.set)
        self.transcript.pack(fill='both', expand=True)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.bind('<Escape>', lambda _e: self.close())
        app.present_modal(self, app)
        self.grab_set()
        def resume():
            sessions = runtime.service.list_sessions()
            if not sessions:
                return runtime.service.new_session()
            sid = sessions[0]['session_id']
            response = runtime.service.get_pending_operations(sid)
            response['messages'] = runtime.service.get_history(sid)['messages']
            return response
        self.request(resume)

    def request(self, function):
        if self.busy:
            return
        self.busy = True
        self.status.set('正在处理… 单事项按策略保存，删除需另行确认。')
        self.send_button.configure(state='disabled')
        self.confirm_button.configure(state='disabled')
        self.cancel_button.configure(state='disabled')
        self.runtime.submit(function, self.display)

    def append(self, text):
        self.transcript.configure(state='normal')
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
            self.sessions.configure(values=[s['label'] for s in response['sessions']])
            if self.session_id in self.session_ids:
                self.sessions.current(self.session_ids.index(self.session_id))
        if response.get('error_message'):
            self.status.set('本次请求未完成；详细提示见对话区。')
            self.append('系统：'+response['error_message'])
        else:
            self.status.set('状态：'+response.get('status', 'ok'))
            if response.get('assistant_message'):
                self.append('助理：'+response['assistant_message'])
        if response.get('messages') is not None:
            self.transcript.configure(state='normal')
            self.transcript.delete('1.0', 'end')
            self.transcript.configure(state='disabled')
            for item in response['messages']:
                self.append(('你：' if item['role'] == 'user' else '助理：')+item['content'])
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
        self.cancel_button.configure(state='normal' if pending else 'disabled')
        if self.operation and self.operation['status'] == 'pending':
            self.render_operation(self.operation)
        if response.get('operation_result'):
            for item in response['operation_result'].get('events',[]):
                self.append(item['date']+' · '+item['title']+' · '+item['color']+(' · 已完成' if item['done'] else ''))
            habit = response['operation_result'].get('habit')
            event = response['operation_result'].get('event')
            self.append('✓ 已'+('更新' if response['operation_result'].get('action') == 'updated' else '创建')+'习惯「'+habit['title']+'」· '+habit['schedule'] if habit else
                        '系统：已删除事项。' if response['operation_result'].get('deleted') else
                        '系统：已实际保存“'+event['title']+'”，日期 '+event['date']+'。' if event else '系统：已核对已执行操作，不重复写入。')
        self.input.focus_set()

    def render_operation(self, operation):
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
        self.append('\n'.join(lines))

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
            self.request(lambda: self.runtime.service.select_candidate(sid, identifier))

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

    def close(self):
        if self.grab_current() is self:
            self.grab_release()
        self.runtime.window = None
        self.destroy()


class AISettings(tk.Toplevel):
    def __init__(self, chat):
        super().__init__(chat)
        self.withdraw()
        self.chat, self.app = chat, chat.app
        self.configuration = chat.runtime.configuration
        self.old = self.configuration.load()
        theme, dp = self.app.theme, self.app.dpi.px
        self.title('AI 设置与隐私')
        self.configure(bg=theme.panel_background)
        self.inputs = {}
        actions = tk.Frame(self, bg=theme.panel_background)
        actions.pack(side='bottom', fill='x', padx=dp(12), pady=dp(10))
        reminder_button(actions, self.app, '保存', self.save, primary=True).pack(side='right')
        reminder_button(actions, self.app, '取消', self.close).pack(side='right', padx=dp(6))
        reminder_button(actions, self.app, '复制本机API Token', self.copy_token).pack(side='left')
        for key, label in [('endpoint', 'API Endpoint（HTTPS）'), ('model', 'Model'), ('api_key', 'API Key（留空保留已有）'), ('timeout', 'Timeout（5–60秒）'), ('port', '本机API端口')]:
            tk.Label(self, text=label, bg=theme.panel_background, fg=theme.text_primary).pack(anchor='w', padx=dp(12))
            value = tk.StringVar(value='' if key == 'api_key' else str(self.old[key]))
            self.inputs[key] = value
            tk.Entry(self, textvariable=value, show='*' if key == 'api_key' else '', bg=theme.input_background,
                     fg=theme.text_primary, insertbackground=theme.text_primary, width=50).pack(fill='x', padx=dp(12), pady=dp(5))
        self.consent = tk.BooleanVar(value=self.old['consent'])
        self.http = tk.BooleanVar(value=self.old['http_enabled'])
        for text, variable in [('我同意将必要消息和查询到的部分日历信息发送给配置的远程模型服务。不会上传全日历或备份。', self.consent),
                               ('启用本机HTTP API（仅127.0.0.1，必须Bearer认证）', self.http)]:
            themed_check(self, theme, dp, text=text, variable=variable, wraplength=dp(430),
                         bg=theme.panel_background, fg=theme.text_primary, selectcolor=theme.input_background).pack(fill='x', padx=dp(12), pady=dp(5))
        self.status = tk.StringVar()
        tk.Label(self, textvariable=self.status, wraplength=dp(430), bg=theme.panel_background, fg=theme.text_secondary).pack(fill='x', padx=dp(12))
        width, height, x, y = dialog_geometry(self.app.dpi.work_area(), self.app.dpi.dpi,
            (chat.winfo_rootx(), chat.winfo_rooty(), chat.winfo_width(), chat.winfo_height()))
        self.geometry(f'{width}x{height}{x:+d}{y:+d}')
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.bind('<Escape>', lambda _e: self.close())
        self.app.present_modal(self, chat)
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
            self.chat.grab_set()
            self.chat.input.focus_set()

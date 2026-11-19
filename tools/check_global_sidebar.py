"""Windows smoke for the right workspace against a synthetic Store."""
import argparse
import ctypes
from ctypes import wintypes as w
from datetime import date
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp, GLOBAL_MIN_WIDTH, GLOBAL_MIN_HEIGHT
from calendar_core import Event, Store
from desktop_calendar.global_view.global_timeline_ui import GLOBAL_TIMELINE_LAYOUT
from tools.check_calendar_chat import wait
from tools.check_interactions import cleanup, pump


class DeleteProvider:
    def __init__(self, event_id):
        self.event_id = event_id

    def complete(self, messages, tools):
        return {'tool_calls': [{'id': 'sidebar-query', 'type': 'function', 'function': {
            'name': 'query_events', 'arguments': json.dumps({'keyword': '隔离删除样例'})}},
            {'id': 'sidebar-delete', 'type': 'function', 'function': {
            'name': 'preview_delete', 'arguments': json.dumps({'event_id': self.event_id})}}]}


def label_texts(widget):
    for child in widget.winfo_children():
        if 'text' in child.keys():
            yield str(child.cget('text'))
        yield from label_texts(child)


def inside(child, outer, *, minimum_width=None):
    assert child.winfo_viewable(), str(child)
    assert child.winfo_width() >= (child.winfo_reqwidth() if minimum_width is None else minimum_width), (
        str(child), child.winfo_width(), child.winfo_reqwidth())
    assert child.winfo_rootx() + child.winfo_width() <= outer.winfo_rootx() + outer.winfo_width(), str(child)
    assert child.winfo_rooty() + child.winfo_height() <= outer.winfo_rooty() + outer.winfo_height(), str(child)


def clickable(button):
    # Geometry alone missed buttons painted underneath a sibling frame.
    x = button.winfo_rootx() + button.winfo_width() // 2
    y = button.winfo_rooty() + button.winfo_height() // 2
    assert button.winfo_containing(x, y) is button, str(button)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', type=Path)
    parser.add_argument('--theme', choices=('modern', 'slate', 'aero', 'paper', 'frutiger'), default='modern')
    args = parser.parse_args()
    if args.screenshots:
        from tools.check_habit_schedules import capture_own_window
        args.screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='calendar-right-workspace-') as folder:
        store = Store(Path(folder) / 'data.json')
        today = date.today()
        # Two days inside the same month and only synthetic inputs.
        first, second = today.replace(day=2), today.replace(day=3)
        store.events = [Event(f'day-{i}', f'当日样例 {i}', f'{first.isoformat()}T09:00:00') for i in range(28)]
        store.events += [Event('delete-me', '隔离删除样例', f'{second.isoformat()}T12:00:00')]
        store.settings.update(theme=args.theme, global_view_hotkey='Ctrl+Alt+F20', assistant_hotkey='Ctrl+Alt+F19')
        store.save()
        with patch.object(CalendarApp, 'check_reminders'):
            app = CalendarApp(store)
            errors = []
            app.report_callback_exception = lambda *parts: errors.append(str(parts))
            try:
                app.enter_global_view()
                app.set_global_display_mode('timeline')
                app.select_day(first)
                area = app.dpi.work_area()
                width = min(app.dpi.px(1210), area.width - app.dpi.px(10))
                height = min(app.dpi.px(600), area.height - app.dpi.px(10))
                app.geometry(f'{width}x{height}{area.left:+d}{area.top:+d}')
                pump(app, .25)
                side = app.global_detail_frame
                side.show()
                pump(app, .15)
                full_geometry = app.geometry()
                narrow_geometry = (f'{app.dpi.px(GLOBAL_MIN_WIDTH)}x{app.dpi.px(GLOBAL_MIN_HEIGHT)}'
                                   f'{area.left:+d}{area.top:+d}')
                logical_width = app.dpi.logical(app.global_workspace.winfo_width())
                assert side.winfo_width() == app.dpi.px(GLOBAL_TIMELINE_LAYOUT.detail_panel_width(logical_width))
                if app.dpi.logical(app.global_workspace.winfo_width()) >= side.DOCK_BELOW:
                    assert side.winfo_manager() == 'grid'
                    old_width = side.winfo_width()
                    side._resize_from_left(SimpleNamespace(x_root=side.winfo_rootx()-app.dpi.px(18)))
                    pump(app)
                    assert side.winfo_width() > old_width
                wait(app, lambda: '当日样例 27' in set(label_texts(side.rows)), 'daily sidebar populated')
                quick_y = app.quick_entry.winfo_rooty()
                side.canvas.yview_moveto(1.0)
                app.update_idletasks()
                assert 0 < side.canvas.yview()[0] < 1
                assert app.quick_entry.winfo_rooty() == quick_y
                inside(app.quick_options_button, side)
                assert app.quick_entry.winfo_viewable()

                # Click the Timeline date header, not the underlying selection helper.
                header = app.global_date_canvas
                x = app._global_day_width * (second.day - 0.5) - header.canvasx(0)
                header.event_generate('<Button-1>', x=int(x), y=app.dpi.px(12))
                pump(app, .15)
                assert app.selected == second
                assert '隔离删除样例' in set(label_texts(side.rows))
                assert '当日样例 0' not in set(label_texts(side.rows))
                app.quick_entry.focus_force()
                pump(app)
                app.quick_placeholder_active = False
                app.quick_var.set('侧栏快速添加')
                app.quick_entry.event_generate('<Return>')
                pump(app, .2)
                assert '侧栏快速添加' in set(label_texts(side.rows))
                assert any(item.title == '侧栏快速添加' for item in store.events_on(second))

                app.set_global_display_mode('flow')
                pump(app, .2)
                # Calendar Flow resolves a blank cell by week and column.
                empty_day = second.replace(day=4)
                week, column = next((i, j) for i, row in enumerate(app.calendar_flow_layout.weeks)
                                    for j, day in enumerate(row.dates) if day == empty_day)
                flow = app.global_flow_canvas
                flow.yview_moveto(0)
                x = (column + .5) * app._global_flow_column_width - flow.canvasx(0)
                y = week * app._global_flow_week_height + app.dpi.px(12) - flow.canvasy(0)
                flow.event_generate('<ButtonPress-1>', x=int(x), y=int(y))
                flow.event_generate('<ButtonRelease-1>', x=int(x), y=int(y))
                pump(app)
                assert app.selected == empty_day and '这一天很清静' in set(label_texts(side.rows))
                side.switch('assistant', focus=True)
                chat, runtime = side.chat, app.chat_runtime
                wait(app, lambda: not chat.busy and chat.session_id, 'conversation ready')
                assert not app.quick_entry.winfo_viewable() and not app.quick_options_button.winfo_viewable()
                assert not chat.candidate.winfo_viewable() and not chat.confirm_button.winfo_viewable()
                assert '还没有消息' in chat.transcript.get('1.0', 'end')
                clickable(chat.send_button)
                if args.screenshots:
                    capture_own_window(app, args.screenshots / 'assistant-idle.png')
                full_label = '这个很长的中文会话名称不应挤走右边的设置按钮' * 3
                chat.display({'sessions': [{'session_id': chat.session_id, 'label': full_label}],
                              'messages': [{'role': 'user', 'content': '一条较长的中文提问' * 20},
                                           {'role': 'assistant', 'content': '一条较长的中文答复' * 35}]})
                pump(app)
                assert chat.sessions.get().endswith('…') and full_label in chat.sessions['values']
                assert chat.transcript.tag_ranges('user') and chat.transcript.tag_ranges('assistant')
                assert chat.transcript.yview()[0] > 0
                clickable(chat.settings_button)
                if args.screenshots:
                    capture_own_window(app, args.screenshots / 'assistant-long.png')
                chat.display({'status': 'clarification_required', 'habit_candidates': [
                    {'title': '隔离习惯', 'schedule': '每天', 'object_type': 'habit'}]})
                pump(app)
                inside(chat.candidate, side, minimum_width=app.dpi.px(100))
                assert not chat.confirm_button.winfo_viewable()
                chat.resume(chat.session_id)
                wait(app, lambda: not chat.busy, 'resume after candidate layout check')
                assert not chat.candidate.winfo_viewable()
                sid = chat.session_id
                chat.new_button.invoke()
                wait(app, lambda: not chat.busy and chat.session_id != sid, 'new conversation')
                chat.sessions.current(chat.session_ids.index(sid))
                chat.sessions.event_generate('<<ComboboxSelected>>')
                wait(app, lambda: not chat.busy and chat.session_id == sid, 'choose conversation')
                chat.settings_button.invoke()
                settings = app.grab_current()
                assert settings.chat is chat and str(settings.transient()) == str(app)
                settings.close()
                assert app.grab_current() is None

                runtime.service.provider = DeleteProvider('delete-me')
                def request_delete():
                    chat.input.insert('1.0', '删除事项「隔离删除样例」')
                    chat.send_button.invoke()
                    wait(app, lambda: not chat.busy, 'delete preview')
                    assert chat.operation and chat.operation['status'] == 'pending', chat.transcript.get('1.0', 'end')
                    assert store.event_by_id('delete-me') is not None
                    for button in (chat.send_button, chat.confirm_button, chat.cancel_button):
                        clickable(button)
                request_delete()
                side.preferred_width = None
                app.geometry(narrow_geometry)
                pump(app, .2)
                assert side.winfo_manager() == 'place'
                assert side.winfo_width() == app.dpi.px(side.MIN_WIDTH)
                active_tab = side.tabs['assistant']
                if app.theme.rounded_widget_shells:
                    _, _, right, _ = active_tab.bbox('all')
                    assert abs(active_tab.winfo_width() - right) <= app.dpi.px(4)
                assert chat.transcript.winfo_height() >= app.dpi.px(15)
                for button in (chat.send_button, chat.confirm_button, chat.cancel_button):
                    inside(button, side)
                    clickable(button)
                if args.screenshots:
                    capture_own_window(app, args.screenshots / 'assistant-confirm.png')
                chat.cancel_button.invoke()
                wait(app, lambda: not chat.busy, 'cancel delete')
                assert store.event_by_id('delete-me') is not None
                assert not chat.confirm_button.winfo_viewable()
                app.geometry(full_geometry)
                pump(app, .2)
                request_delete()
                chat.confirm_button.invoke()
                wait(app, lambda: not chat.busy, 'confirm delete')
                assert store.event_by_id('delete-me') is None
                chat.clear_button.invoke()
                wait(app, lambda: not chat.busy and chat.session_id != sid, 'clear and rebuild conversation')

                chat.input.insert('1.0', '未发送草稿')
                side.switch('day')
                pump(app)
                assert app.quick_entry.winfo_viewable()
                assert '隔离删除样例' not in set(label_texts(side.rows))
                side.switch('assistant')
                assert side.chat is chat and chat.input.get('1.0', 'end-1c') == '未发送草稿'
                wait(app, lambda: not chat.busy, 'resume embedded chat')

                side.preferred_width = None
                app.geometry(narrow_geometry)
                pump(app, .25)
                assert side.winfo_manager() == 'place'
                assert side.winfo_width() == app.dpi.px(GLOBAL_TIMELINE_LAYOUT.detail_min_width)
                for widget in (chat.new_button, chat.clear_button, chat.settings_button, chat.send_button):
                    inside(widget, side)
                    clickable(widget)
                assert not chat.candidate.winfo_viewable() and not chat.confirm_button.winfo_viewable()
                inside(chat.sessions, side, minimum_width=app.dpi.px(38))
                assert not app.quick_entry.winfo_viewable()
                for widget in (chat.input, chat.transcript):
                    assert widget.winfo_viewable() and widget.winfo_height() > app.dpi.px(15), (
                        str(widget), widget.winfo_height(), chat.winfo_height(), app.winfo_height())
                if args.screenshots:
                    capture_own_window(app, args.screenshots / 'assistant-narrow.png')
                side.toggle_drawer()
                pump(app)
                assert not side.winfo_manager()
                app._focus_quick_entry()
                pump(app)
                assert side.winfo_manager() == 'place' and side.mode == 'day' and app.quick_entry.winfo_viewable()
                inside(app.quick_entry, side, minimum_width=app.dpi.px(60))
                inside(app.quick_options_button, side)
                clickable(app.quick_options_button)
                if args.screenshots:
                    capture_own_window(app, args.screenshots / 'day-narrow.png')

                # The earlier editable shortcut work, when present, is exercised
                # through the native helper; the layout also works without it.
                tray = app.tray_icon
                binding = getattr(tray, '_hotkey_binding', None)
                active = getattr(binding, 'active', None)
                if isinstance(active, dict) and 'assistant' in active:
                    identifier, shortcut = active['assistant']
                    user = ctypes.WinDLL('user32')
                    user.PostMessageW.argtypes = [w.HWND, w.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
                    assert user.PostMessageW(tray._hwnd, tray.WM_HOTKEY, identifier,
                                             (shortcut.key << 16) | shortcut.modifiers)
                    via = 'WM_HOTKEY'
                else:
                    from desktop_calendar.assistant.calendar_chat_ui import open_chat
                    open_chat(app)
                    via = 'menu/action'
                wait(app, lambda: runtime.window is not None and not runtime.window.busy, 'standalone chat ready')
                window = runtime.window
                assert window is not chat and app.grab_current() is window and window.runtime is runtime
                window.close()
                assert app.grab_current() is None and side.chat is chat
                assert not errors, errors
                print(json.dumps(dict(theme=args.theme, dpi=app.dpi.dpi, day_scroll=True, quick_add=True,
                                      chat_confirm_cancel=True, narrow_drawer=True, standalone=via)), flush=True)
            finally:
                blocker = app._active_modal_window()
                if blocker and hasattr(blocker, 'close'):
                    blocker.close()
                cleanup(app)


if __name__ == '__main__':
    main()

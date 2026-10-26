"""Isolated reminder-window visual/layout QA. Fake transport, no native sends."""
from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import queue
import sys
import tempfile
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from calendar_core import Event, Store
from dpi_utils import WorkArea, dpi_for_window
from reminder_engine import STATE_KEY, plans_for_store
from reminder_overview import ReminderOverview
from tools.check_habit_schedules import capture_own_window
from tools.check_interactions import cleanup, pump
from tools.check_reminder_ui import visible


def main():
    output = Path(__file__).resolve().parents[1] / 'build/reminder-polish'
    output.mkdir(parents=True, exist_ok=True)
    errors, cases = [], []
    with tempfile.TemporaryDirectory(prefix='calendar-reminder-visuals-') as directory:
        store = Store(Path(directory) / 'data.json')
        due = (datetime.now() + timedelta(days=20)).date().isoformat() + 'T18:00'
        titles = ['核对项目资料 · Review documents', '完成设计提案 🔔 Design review',
                  '准备下周出行 · Travel checklist', '很长的中英文事项标题 Long imported title 🔔 ' * 12]
        store.events = [Event(str(i), titles[i % len(titles)] + f' {i+1}', due, has_time=False,
                              event_type='ddl' if i % 3 == 0 else 'general') for i in range(40)]
        with patch.object(CalendarApp, '_start_tray_icon'), patch.object(CalendarApp, 'check_reminders'):
            app = CalendarApp(store)
            app.report_callback_exception = lambda *parts: errors.append(str(parts))
            try:
                pump(app, .2)
                native = dpi_for_window(app)
                backend = Mock(events=queue.Queue(), status='隔离测试服务 · 未发送真实通知', _thread=None)
                app.notification_backend = backend
                app.reminder_engine.backend = backend
                app.reminder_engine.configure({'enabled': True})
                plans = {p.event_id: p for p in plans_for_store(store, app.reminder_engine.config())}
                state = app.reminder_engine._state()
                state['deliveries'][plans['0'].key] = {'status': 'failed', 'attempts': 1, 'at': datetime.now().isoformat()}
                app.reminder_engine._save(STATE_KEY, state)
                app.reminder_engine.snooze('1', plans['1'].key, 30, datetime.now())
                before, facts = store.data_file.read_bytes(), [asdict(event) for event in store.events]
                for dpi in (96, 120, 144, 168, 192):
                    with patch.object(app.dpi, 'current_window_dpi', return_value=dpi):
                        app._apply_dpi_change(dpi)
                        for theme in ('modern', 'aero', 'paper', 'frutiger'):
                            app.set_theme(theme)
                            pump(app, .08)
                            case_before = store.data_file.read_bytes()
                            app.open_reminder_settings()
                            window = app.notification_settings_window
                            pump(app, .1)
                            assert len(window.sections) == 4
                            assert visible(window.save_button, window)
                            if dpi == native:
                                capture_own_window(window, output / f'{theme}-settings.png')
                            window.scroll_canvas.yview_moveto(1)
                            pump(app, .06)
                            window.event_choice.set(next(iter(window.event_labels)))
                            window.select_event()
                            window.rule_mode.set('自定义日期提醒')
                            window.update_custom()
                            window.update_idletasks()
                            window.scroll_canvas.yview_moveto(1)
                            pump(app, .06)
                            assert visible(window.cancel_button, window)
                            if dpi == native:
                                capture_own_window(window, output / f'{theme}-settings-rules.png')
                            window.close()
                            app.open_reminder_plans()
                            window = app.notification_plan_window
                            pump(app, .1)
                            assert visible(window.close_button, window)
                            assert len(window.rows) == 40
                            window.tree.selection_set('event:3')
                            window.show_selection()
                            assert store.events[3].title in window.detail.get()
                            assert store.events[3].title in window.detail_label.get('1.0', 'end')
                            window.tree.selection_set('event:1')
                            window.show_selection()
                            assert window.cancel_button.cget('state') == 'normal'
                            if dpi == native:
                                capture_own_window(window, output / f'{theme}-plans.png')
                            for _ in range(15):
                                window._hover(type('Point', (), {'y': 80})())
                            if window._hovered:
                                assert window.tree.item(window._hovered, 'tags').count('hover') <= 1
                            window.refresh()
                            assert window.tree.selection() == ('event:1',)
                            window.tree.yview_moveto(1)
                            pump(app, .05)
                            assert window.tree.bbox(window.tree.get_children('upcoming')[-1])
                            with patch.object(app.reminder_engine, 'overview', return_value=ReminderOverview(True, ())):
                                window.refresh()
                                pump(app, .05)
                                assert window.empty_label.winfo_ismapped()
                                if dpi == native:
                                    capture_own_window(window, output / f'{theme}-empty.png')
                            window.close()
                            assert app.grab_current() is None
                            assert case_before == store.data_file.read_bytes(), 'Reminder windows wrote data'
                            cases.append((dpi, theme))
                with patch.object(app.dpi, 'work_area', return_value=WorkArea(0, 0, 1280, 800)):
                    app.open_reminder_plans()
                    window = app.notification_plan_window
                    pump(app, .1)
                    assert window.winfo_height() <= 800 and visible(window.close_button, window)
                    capture_own_window(window, output / 'small-work-area.png')
                    assert window.tree.winfo_height() >= app.dpi.px(3 * 29), ('Compact footer squeezed out reminder rows', window.tree.winfo_height())
                    window.tree.yview_moveto(1)
                    pump(app, .05)
                    assert window.tree.bbox(window.tree.get_children('upcoming')[-1])
                    capture_own_window(window, output / 'small-work-area.png')
                    window.close()
                original = json.loads(before)
                current = json.loads(store.data_file.read_bytes())
                for key in ('native_reminders', STATE_KEY):
                    assert original['settings'][key] == current['settings'][key]
                assert facts == [asdict(event) for event in store.events]
                assert not errors, errors
                backend.send.assert_not_called()
                result = dict(native_dpi=native, theme_dpi_cases=len(cases), unchanged_data=True,
                              no_native_sends=True, errors=errors)
                (output / 'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
                print(json.dumps(result), flush=True)
            finally:
                for window in (app.notification_settings_window, app.notification_plan_window):
                    if window and window.winfo_exists():
                        window.close()
                cleanup(app)


if __name__ == '__main__':
    main()

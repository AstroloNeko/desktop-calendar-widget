"""Windows visual smoke for settings and Tk cascades, with synthetic data only."""
import argparse
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from desktop_preferences import ACTIVE_THEME_NAMES
from calendar_core import Store
from desktop_calendar.assistant.calendar_chat_ui import AISettings
from desktop_calendar.reminders.reminder_ui import ReminderSettingsDialog
from desktop_calendar.ui.themed_controls import RoundedCardShell
from global_hotkey_ui import HotkeyDialog
from tools.check_global_sidebar import inside
from tools.check_habit_schedules import capture_own_window
from tools.check_interactions import cleanup, pump


def clickable(button):
    dialog = button.winfo_toplevel()
    for _ in range(3):
        x = button.winfo_rootx() + button.winfo_width() // 2
        y = button.winfo_rooty() + button.winfo_height() // 2
        hit = button.winfo_containing(x, y)
        if hit is button:
            return
        if hit is not None and hit.winfo_toplevel() is dialog:
            break
        dialog.lift()
        pump(dialog, .15)
    raise AssertionError((str(button), str(hit), (x, y), dialog.geometry()))


def check_cascades(app, command, count):
    posted = []
    with patch.object(tk.Menu, 'tk_popup', lambda menu, *_args: posted.append(menu)):
        command()
    menu, = posted
    cascades = [menu.nametowidget(menu.entrycget(index, 'menu'))
                for index in range(menu.index('end') + 1) if menu.type(index) == 'cascade']
    assert len(cascades) == count
    for control in (menu, *cascades):
        assert str(control.cget('background')) == app.theme.panel_background, control.cget('background')
        assert str(control.cget('activebackground')) == app.theme.accent_soft
        assert control.index('end') is not None
    return menu, cascades


def run(theme, screenshots):
    with tempfile.TemporaryDirectory(prefix='calendar-settings-qa-') as folder:
        store = Store(Path(folder) / 'calendar.json')
        store.settings.update(theme=theme)
        store.save()
        with patch.object(CalendarApp, '_start_tray_icon'), patch.object(CalendarApp, 'check_reminders'):
            app = CalendarApp(store)
            errors = []
            app.report_callback_exception = lambda *args: errors.append(args)
            try:
                app.enter_global_view()
                pump(app)
                area, dp = app.dpi.work_area(), app.dpi.px
                app.geometry(f'{min(dp(1120), area.width)}x{min(dp(590), area.height)}{area.left:+d}{area.top:+d}')
                pump(app)
                app.global_detail_frame.show()
                for command, count in ((app.show_main_menu, 3), (app.show_quick_options, 2)):
                    menu, children = check_cascades(app, command, count)
                    if count == 2:
                        event_types = children[1]
                        index = next(i for i in range(event_types.index('end')+1)
                                     if event_types.entrycget(i, 'value') == 'urgent')
                        event_types.invoke(index)
                        assert app.quick_event_type == 'urgent'
                    menu.destroy()

                app.global_detail_frame.show(mode='assistant')
                chat = app.global_detail_frame.chat
                fake = dict(endpoint='https://example.invalid/v1', model='example-model', api_key='',
                            timeout=30, port=18746, consent=False, http_enabled=False)
                forms = (('ai', lambda: AISettings(chat)), ('shortcuts', lambda: HotkeyDialog(app)),
                         ('reminders', lambda: ReminderSettingsDialog(app)))
                for name, factory in forms:
                    with patch.object(chat.runtime, 'configuration', SimpleNamespace(load=lambda: fake.copy())):
                        dialog = factory()
                    pump(app, .18)
                    assert app.grab_current() is dialog
                    inside(dialog.save_button, dialog)
                    clickable(dialog.save_button)
                    if name == 'ai':
                        assert dialog.fields['api_key'].cget('show') == '*'
                        assert isinstance(dialog.fields['endpoint'].master, RoundedCardShell) == app.theme.rounded_widget_shells
                        if dialog.winfo_height() >= dp(485):
                            clickable(dialog.copy_button)
                    if screenshots:
                        output = screenshots / theme
                        output.mkdir(parents=True, exist_ok=True)
                        capture_own_window(dialog, output / (name + '.png'))
                    dialog.geometry(f'{dp(320)}x{dp(270)}')
                    pump(app, .12)
                    inside(dialog.save_button, dialog)
                    clickable(dialog.save_button)
                    if name == 'ai':
                        dialog.fields['port'].focus_force()
                        pump(app)
                        canvas = dialog.form.canvas
                        assert canvas.yview()[0] > 0
                        assert dialog.fields['port'].winfo_rooty() + dialog.fields['port'].winfo_height() <= (
                            canvas.winfo_rooty() + canvas.winfo_height())
                        clickable(dialog.save_button)
                        if screenshots:
                            capture_own_window(dialog, output / 'ai-narrow.png')
                    dialog.close()
                    pump(app)
                    assert app.grab_current() is None
                assert not errors, errors
                print(theme, app.dpi.dpi, 'AI / shortcuts / reminders / cascades PASS', flush=True)
            finally:
                active = app._active_modal_window()
                if active is not None:
                    active.close()
                cleanup(app)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--themes', nargs='+', choices=ACTIVE_THEME_NAMES, default=ACTIVE_THEME_NAMES)
    parser.add_argument('--screenshots', type=Path)
    args = parser.parse_args()
    for theme in args.themes:
        run(theme, args.screenshots)

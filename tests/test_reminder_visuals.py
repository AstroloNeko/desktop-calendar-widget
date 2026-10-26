"""Presentation-only control contracts; no calendar or notification service."""
import tkinter as tk
from types import SimpleNamespace
import unittest

from reminder_ui import reminder_button
from ui_theme import get_theme


class ReminderControlTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def app(self, theme):
        return SimpleNamespace(theme=get_theme(theme), dpi=SimpleNamespace(px=lambda value: round(value)),
                               search_font='Segoe UI')

    def test_four_theme_primary_control_uses_semantic_colors(self):
        for name in ('modern', 'aero', 'paper', 'frutiger'):
            app = self.app(name)
            button = reminder_button(self.root, app, '保存设置', lambda: None, primary=True)
            self.assertEqual(button.cget('bg'), app.theme.accent)
            self.assertEqual(button.cget('fg'), app.theme.text_on_accent)
            self.assertEqual(button.cget('highlightcolor'), app.theme.input_focus)
            button.destroy()

    def test_disabled_control_does_not_invoke_action(self):
        called = []
        app = self.app('paper')
        button = reminder_button(self.root, app, '发送测试通知', lambda: called.append(True))
        button.configure(state='disabled')
        button.invoke()
        self.assertEqual(called, [])
        self.assertEqual(button.cget('disabledforeground'), app.theme.text_disabled)

    def test_secondary_control_keeps_existing_callback(self):
        called = []
        app = self.app('aero')
        button = reminder_button(self.root, app, '打开事项', lambda: called.append(True))
        self.assertEqual(button.cget('bg'), app.theme.control_background)
        button.invoke()
        self.assertEqual(called, [True])

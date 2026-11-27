from types import SimpleNamespace
from datetime import datetime
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

from desktop_calendar.ui import themed_controls
from desktop_calendar.reminders.reminder_center import ReminderPlanPanel
from desktop_calendar.reminders.reminder_ui import ReminderSettingsDialog
from desktop_calendar.ui.ui_theme import get_theme


class ThemedDialogControlsTests(unittest.TestCase):
    def test_rounded_title_keeps_a_native_entry_bound_to_the_original_variable(self):
        for name in ("modern", "slate"):
            app = SimpleNamespace(theme=get_theme(name), dpi=SimpleNamespace(px=lambda n: n * 2),
                                  search_font="UI")
            parent, variable = Mock(), Mock()
            with patch.object(themed_controls, "RoundedCardShell") as shell, \
                    patch.object(themed_controls.tk, "Entry") as entry:
                field, surface = themed_controls.form_entry(parent, app, variable, font_size=11)
                self.assertIs(field, entry.return_value)
                self.assertIs(surface, shell.return_value)
                self.assertIs(entry.call_args.kwargs["textvariable"], variable)
                self.assertEqual(entry.call_args.kwargs["font"], ("UI", 11))

    def test_delete_button_keeps_the_native_command_and_danger_tokens(self):
        for name in ("modern", "slate"):
            theme = get_theme(name)
            app = SimpleNamespace(theme=theme, dpi=SimpleNamespace(px=lambda n: n * 2), search_font="UI")
            command = Mock()
            state = {"state": "normal"}
            def configure(cnf=None, **options):
                state.update(cnf or {}, **options)
            with patch.object(themed_controls, "RoundedCardShell") as shell, \
                    patch.object(tk.Button, "__init__", return_value=None) as native, \
                    patch.object(tk.Button, "configure", side_effect=configure), \
                    patch.object(tk.Button, "cget", side_effect=state.__getitem__), \
                    patch.object(tk.Button, "bind"), patch.object(tk.Button, "pack"):
                control, surface = themed_controls.form_button(Mock(), app, "删除", command, destructive=True)
                self.assertIsInstance(control, tk.Button)
                self.assertIs(surface, shell.return_value)
                self.assertIs(native.call_args.kwargs["command"], command)
                self.assertEqual(state["fg"], theme.danger)
                self.assertEqual(state["bg"], theme.danger_soft)
                surface.set_outline.assert_called_with(theme.event_type_ddl_border)
                control.configure({"state": "disabled"})
                control._track("_hovered", True)
                self.assertEqual((state["bg"], state["fg"], state["cursor"]),
                                 (theme.control_disabled, theme.text_disabled, ""))
                surface.set_fill.assert_called_with(theme.control_disabled)
                control.config(state="normal")
                self.assertEqual(state["cursor"], "hand2")

    def test_combobox_state_and_option_config_both_repaint_its_shell(self):
        combo = object.__new__(themed_controls._FormCombobox)
        combo._paint_surface = Mock()
        with patch.object(ttk.Combobox, "configure") as configure, patch.object(ttk.Combobox, "state") as state:
            combo.configure(state="disabled")
            combo.state(("!disabled",))
            combo.configure(values=("10 天",))
            combo.state()
        self.assertEqual(combo._paint_surface.call_count, 2)
        self.assertEqual(configure.call_args_list[0].kwargs, {"state": "disabled"})
        self.assertEqual(state.call_args_list[0].args, (("!disabled",),))

    def test_segmented_strip_retains_notebook_pages_and_selection_callback(self):
        theme = get_theme("slate")
        app = SimpleNamespace(theme=theme, dpi=SimpleNamespace(px=lambda n: n))
        pages = [Mock(), Mock()]
        strip, divider = Mock(), Mock()
        buttons = [Mock(), Mock()]
        with patch.object(themed_controls.tk, "Frame", side_effect=[strip, divider, *pages]), \
                patch.object(themed_controls.ttk, "Style"), \
                patch.object(themed_controls.ttk, "Notebook") as notebook_factory, \
                patch.object(themed_controls, "form_button",
                             side_effect=[(button, Mock()) for button in buttons]) as build:
            notebook = notebook_factory.return_value
            notebook.select.return_value = str(pages[0])
            tabs, returned_pages = themed_controls.build_segmented_tabs(Mock(), app, ("甲", "乙"))
            self.assertIs(tabs, notebook)
            self.assertEqual(returned_pages, pages)
            self.assertEqual([args.args[0] for args in notebook.add.call_args_list], pages)
            buttons[0].set_primary.assert_called_with(True)
            build.call_args_list[1].args[3]()
            notebook.select.assert_called_with(pages[1])
            notebook.select.return_value = str(pages[1])
            event, paint = notebook.bind.call_args.args
            self.assertEqual(event, "<<NotebookTabChanged>>")
            paint()
            buttons[0].set_primary.assert_called_with(False)
            buttons[1].set_primary.assert_called_with(True)

    def test_brief_preview_keeps_the_complete_context_at_its_natural_width(self):
        theme = get_theme("modern")
        now = datetime(2026, 11, 27, 18)
        item = {"group": "today_ddl", "title": "交付"}
        context = "今天已逾期 17:00"
        context_label = Mock()
        dialog = SimpleNamespace(
            _closing=False, tabs=SimpleNamespace(index=lambda _what: 1),
            _brief_values=lambda: {"upcoming": True}, brief_days=Mock(),
            preview_body=Mock(winfo_children=Mock(return_value=[])),
            _brief_preview_content=lambda _raw: (now, [item], None),
            refresh_brief_test=Mock(), preview_source=Mock(), _preview_slot=lambda: "evening",
            app=SimpleNamespace(reminder_engine=SimpleNamespace(config=lambda: {"private": False})),
            private=SimpleNamespace(get=lambda: False), preview_date=Mock(), preview_heading=Mock(),
            dp=lambda n: n, theme=theme, _divider=Mock(), _note=Mock(),
            _label=Mock(side_effect=lambda _parent, text, **_options: context_label if text == context else Mock()),
        )
        with patch("desktop_calendar.reminders.reminder_ui.tk.Frame"), \
                patch("desktop_calendar.reminders.reminder_ui.brief_heading"), \
                patch("desktop_calendar.reminders.reminder_ui.brief_today_count"), \
                patch("desktop_calendar.reminders.reminder_ui.brief_enabled"), \
                patch("desktop_calendar.reminders.reminder_ui.brief_item_context", return_value=context), \
                patch("desktop_calendar.reminders.reminder_ui.select_brief_items",
                      return_value=([item], {"today_ddl": 1})):
            ReminderSettingsDialog.refresh_preview(dialog)
        context_label.configure.assert_not_called()
        context_label.pack.assert_called_once()

    def test_short_reminder_panel_hides_only_the_empty_detail_placeholder(self):
        card = Mock()
        card.winfo_manager.return_value = ""
        panel = SimpleNamespace(
            _compact=True, EMPTY_DETAIL=ReminderPlanPanel.EMPTY_DETAIL, detail=Mock(),
            detail_card=card, details_title=Mock(), details_content=Mock(),
            detail_label=Mock(), selection_actions=Mock(), metrics=Mock(), selected_row=lambda: None,
            app=SimpleNamespace(dpi=SimpleNamespace(px=lambda n: n)),
        )
        panel.detail.get.return_value = panel.EMPTY_DETAIL
        ReminderPlanPanel._render_detail(panel)
        card.pack_forget.assert_called_once()
        card.pack.assert_not_called()
        panel.detail.get.return_value = "取消未完成：请重试"
        ReminderPlanPanel._render_detail(panel)
        card.pack.assert_called_once()
        panel.details_title.set.assert_called_with("取消未完成：请重试")

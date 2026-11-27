from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from desktop_calendar.ui import themed_controls
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
            with patch.object(themed_controls, "RoundedCardShell") as shell, \
                    patch.object(themed_controls.tk, "Button") as button:
                control, surface = themed_controls.form_button(Mock(), app, "删除", command, danger=True)
                self.assertIs(control, button.return_value)
                self.assertIs(surface, shell.return_value)
                self.assertIs(button.call_args.kwargs["command"], command)
                self.assertEqual(button.call_args.kwargs["fg"], theme.danger)
                self.assertEqual(button.call_args.kwargs["bg"], theme.danger_soft)
                self.assertEqual(shell.call_args.kwargs["outline"], theme.event_type_ddl_border)

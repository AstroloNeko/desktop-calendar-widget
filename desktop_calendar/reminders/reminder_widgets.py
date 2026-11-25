"""Reminder-center controls. Styles are scoped to this dialog."""
import tkinter as tk
from tkinter import ttk
from desktop_calendar.ui.themed_controls import RoundedCardShell


def reminder_font(app, size=9, style="normal"):
    # Device pixels avoid reusing font metrics cached at a different monitor scale.
    return app.search_font, -app.dpi.px(size * 4 / 3), style


class ReminderButton(tk.Button):
    def __init__(self, parent, app, text, command, *, primary=False):
        self.theme = theme = app.theme
        self.primary = primary
        self.hovered = False
        dp = app.dpi.px
        font = reminder_font(app)
        super().__init__(parent, text=text, command=command, font=font, relief="flat", bd=0,
                         highlightthickness=dp(1), highlightcolor=theme.input_focus,
                         disabledforeground=theme.text_disabled, padx=dp(11), pady=0, takefocus=True)
        self.configure(pady=max(0, (dp(35) - self.winfo_reqheight()) // 2))
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))
        self._paint()

    def configure(self, cnf=None, **options):
        result = super().configure(cnf, **options)
        if "state" in options:
            self._paint()
        return result

    def _hover(self, hovered):
        self.hovered = hovered
        self._paint()

    def _paint(self):
        theme = self.theme
        disabled = self.cget("state") == "disabled"
        accent = self.primary and not disabled
        fill = theme.control_disabled if disabled else theme.accent if accent else theme.control_background
        hover = theme.accent_hover if accent else theme.control_hover
        color = theme.text_on_accent if accent else theme.control_text
        super().configure(bg=hover if self.hovered and not disabled else fill, fg=color,
                          activebackground=hover, activeforeground=color, cursor="" if disabled else "hand2",
                          takefocus=not disabled, highlightbackground=theme.accent if accent else theme.divider)


def notebook_style(style, app):
    theme, dp = app.theme, app.dpi.px
    # Match AISettings, with a dialog-specific style name.
    name = "ReminderCenter.TNotebook"
    style.configure(name, background=theme.panel_background, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure(name + ".Tab", font=reminder_font(app), padding=(dp(12), dp(6)))
    style.map(name + ".Tab",
              background=[("selected", theme.accent_soft), ("!selected", theme.panel_background)],
              foreground=[("selected", theme.text_primary), ("!selected", theme.text_secondary)])
    return name


COMBO = "ReminderCenter.TCombobox"


def combo_style(style, app):
    theme, dp = app.theme, app.dpi.px
    for name, source in (("ReminderCombo.field", "Combobox.field"),
                         ("ReminderCombo.downarrow", "Combobox.downarrow")):
        if name not in style.element_names():
            style.element_create(name, "from", "clam", source)
    style.layout(COMBO, [
        ("ReminderCombo.downarrow", {"side": "right", "sticky": "ns"}),
        ("ReminderCombo.field", {"sticky": "nswe", "children": [
            ("Combobox.padding", {"sticky": "nswe", "children": [
                ("Combobox.textarea", {"sticky": "nswe"})]})]})])
    style.configure(COMBO, padding=(dp(8), dp(3)), arrowsize=dp(12), arrowcolor=theme.text_secondary,
                    foreground=theme.text_primary, background=theme.input_background,
                    fieldbackground=theme.input_background, bordercolor=theme.input_border,
                    lightcolor=theme.input_background, darkcolor=theme.input_background)
    style.map(COMBO, fieldbackground=[("disabled", theme.control_disabled), ("readonly", theme.input_background)],
              foreground=[("disabled", theme.text_disabled)],
              bordercolor=[("focus", theme.input_focus)],
              selectbackground=[("readonly", theme.accent_soft)],
              selectforeground=[("readonly", theme.text_primary)])


def card(parent, app, *, padding=16, surface=None):
    theme, dp = app.theme, app.dpi.px
    fill = surface or theme.workspace_detail
    if theme.rounded_widget_shells and padding >= 9:
        border = RoundedCardShell(parent, theme, dp, fill, outline=theme.divider,
                                  surface=parent.cget("bg"), cursor="arrow")
        inset = dp(2)
    else:
        border = tk.Frame(parent, bg=theme.divider, bd=0, highlightthickness=0)
        inset = dp(1)
    body = tk.Frame(border, bg=fill, padx=dp(padding), pady=dp(padding), bd=0, highlightthickness=0)
    body.pack(fill="both", expand=True, padx=inset, pady=inset)
    return border, body

"""Reminder-center controls. Styles are scoped to this dialog."""
import tkinter as tk
from desktop_calendar.ui.themed_controls import RoundedCardShell


def reminder_font(app, size=9, style="normal"):
    # Device pixels avoid reusing font metrics cached at a different monitor scale.
    return app.search_font, -app.dpi.px(size * 4 / 3), style


def card(parent, app, *, padding=16, surface=None):
    theme, dp = app.theme, app.dpi.px
    fill = surface or theme.workspace_detail
    if theme.rounded_widget_shells:
        border = RoundedCardShell(parent, theme, dp, fill, outline=theme.divider,
                                  surface=parent.cget("bg"), cursor="arrow",
                                  radius=dp(7 if padding <= 8 else 10))
        inset = dp(2)
    else:
        border = tk.Frame(parent, bg=theme.divider, bd=0, highlightthickness=0)
        inset = dp(1)
    body = tk.Frame(border, bg=fill, padx=dp(padding), pady=dp(padding), bd=0, highlightthickness=0)
    body.pack(fill="both", expand=True, padx=inset, pady=inset)
    return border, body

import tkinter as tk
from tkinter import ttk
import unittest

from themed_controls import RoundedCardShell, configure_scrollbars, themed_check
from ui_theme import get_theme


class SlateControlTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.addCleanup(self.root.destroy)
        self.theme = get_theme("slate")
        self.dp = lambda value: round(2 * value)

    def test_rounded_card_reuses_surface_on_resize_and_hover(self):
        shell = RoundedCardShell(self.root, self.theme, self.dp, self.theme.schedule_card_background)
        shell.pack()
        shell.configure(width=140, height=64)
        self.root.update()
        item, = shell.find_withtag("surface")
        before = shell.coords(item)
        shell.set_fill(self.theme.schedule_card_hover)
        self.assertEqual(shell.coords(item), before)
        self.assertEqual(shell.itemcget(item, "fill"), self.theme.schedule_card_hover)
        shell.configure(width=220)
        self.root.update()
        self.assertEqual(shell.find_all(), (item,))
        self.assertNotEqual(shell.coords(item), before)
        self.assertEqual(before[1::2], shell.coords(item)[1::2])

    def test_native_keyboard_variable_and_disabled_behavior(self):
        variable = tk.BooleanVar(self.root)
        calls = []
        check = themed_check(self.root, self.theme, self.dp, text="提醒", variable=variable,
                             bg=self.theme.panel_background, command=lambda: calls.append(variable.get()))
        check.pack()
        self.root.update()
        check.focus_force()
        self.root.update()
        check.event_generate("<KeyPress-space>")
        self.root.update()
        self.assertEqual(calls, [True])
        check.configure(state="disabled")
        check.invoke()
        self.assertEqual(calls, [True])
        variable.set(False)
        self.assertFalse(variable.get())

    def test_scrollbar_native_drag_and_restoration(self):
        root = self.root
        root.geometry("200x240")
        style = ttk.Style(root)
        original = style.layout("Vertical.TScrollbar")
        configure_scrollbars(root, style, self.theme, self.dp)
        for direction in ("Vertical", "Horizontal"):
            base = direction + ".TScrollbar"
            track, options = style.layout("Global." + base)[0]
            self.assertEqual(track, root._theme_scroll_styles[base][0][0])
            self.assertTrue(options["children"][0][0].startswith("Rounded_slate_"))
        listing = tk.Listbox(root)
        bar = ttk.Scrollbar(root, command=listing.yview, style="Global.Vertical.TScrollbar")
        bar.pack(side="right", fill="y")
        listing.pack(fill="both", expand=True)
        listing.configure(yscrollcommand=bar.set)
        listing.insert("end", *range(90))
        root.update()
        x, y = bar.winfo_width()//2, 5
        self.assertTrue(bar.identify(x, y).endswith("thumb"))
        bar.event_generate("<ButtonPress-1>", x=x, y=y)
        bar.event_generate("<B1-Motion>", x=x, y=y+65)
        bar.event_generate("<ButtonRelease-1>", x=x, y=y+65)
        root.update()
        self.assertGreater(listing.yview()[0], 0)
        configure_scrollbars(root, style, get_theme("modern"), self.dp)
        track, options = style.layout("Vertical.TScrollbar")[0]
        self.assertEqual(track, original[0][0])
        self.assertTrue(options["children"][0][0].startswith("Rounded_modern_"))

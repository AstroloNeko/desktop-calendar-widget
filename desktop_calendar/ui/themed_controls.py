"""Shared rounded control artwork. Native widgets retain their class bindings."""
import tkinter as tk
from tkinter import ttk
import math


# A superellipse quadrant (exponent 3) blends into the straight edges with
# vanishing curvature. Sample once, then reflect the same corner four ways.
_ARC = tuple((math.cos(i*math.pi/24)**(2/3), math.sin(i*math.pi/24)**(2/3))
             for i in range(13))


class RoundedCardShell(tk.Canvas):
    def __init__(self, parent, theme, dp, fill, *, outline=None, cursor="hand2", surface=None,
                 radius: int | None = None):
        super().__init__(parent, bd=0, highlightthickness=0,
                         bg=surface or theme.schedule_background, cursor=cursor)
        self._radius = dp(10) if radius is None else radius
        self._size = None
        self._surface = self.create_polygon(0, 0, 0, 0, 0, 0, fill=fill,
                                            outline=theme.schedule_card_border if outline is None else outline,
                                            tags="surface")
        self.bind("<Configure>", self._resize)

    def _resize(self, event):
        size = event.width, event.height
        if size == self._size or min(size) < 3:
            return
        self._size = size
        right, bottom = size[0]-1, size[1]-1
        r = min(self._radius, (right-1)/2, (bottom-1)/2)
        points = []
        for cx, cy, sx, sy, arc in (
                (right-r, 1+r, 1, -1, reversed(_ARC)),
                (right-r, bottom-r, 1, 1, _ARC),
                (1+r, bottom-r, -1, 1, reversed(_ARC)),
                (1+r, 1+r, -1, -1, _ARC)):
            for x, y in arc:
                points.extend((cx+sx*r*x, cy+sy*r*y))
        self.coords(self._surface, *points)

    def set_fill(self, color):
        self.itemconfigure(self._surface, fill=color)

    def set_outline(self, color):
        self.itemconfigure(self._surface, outline=color)


def themed_popup_menu(parent, theme, font):
    """Keep native Tk menu traversal, accelerators, and cascades."""
    return tk.Menu(parent, tearoff=False, font=font, bg=theme.panel_background,
                   fg=theme.text_primary, activebackground=theme.accent_soft,
                   activeforeground=theme.text_primary, disabledforeground=theme.text_disabled,
                   selectcolor=theme.accent)


def form_entry(parent, app, variable, *, show="", width=1, rounded=True, font_size=9):
    """The geometry widget may be a shell; the entry remains a native Tk field."""
    theme, dp = app.theme, app.dpi.px
    shell = (RoundedCardShell(parent, theme, dp, theme.input_background, surface=parent.cget("bg"),
                              outline=theme.input_border, cursor="xterm", radius=dp(7))
             if rounded and theme.rounded_widget_shells else None)
    entry = tk.Entry(shell or parent, textvariable=variable, show=show, width=width,
                     font=(app.search_font, font_size), bg=theme.input_background,
                     fg=theme.text_primary, insertbackground=theme.text_primary,
                     relief="flat", bd=0, highlightthickness=0 if shell else dp(1),
                     highlightbackground=theme.input_border, highlightcolor=theme.input_focus)
    if shell:
        entry.pack(fill="both", expand=True, padx=dp(8), pady=dp(5))
        shell.bind("<Button-1>", lambda _e: entry.focus_set())
        entry.bind("<FocusIn>", lambda _e: shell.set_outline(theme.input_focus))
        entry.bind("<FocusOut>", lambda _e: shell.set_outline(theme.input_border))
    return entry, shell or entry


class _FormButton(tk.Button):
    def __init__(self, parent, app, text, command, *, shell, surface, primary, quiet, destructive, font, compact):
        theme, dp = app.theme, app.dpi.px
        self._theme, self._shell, self._surface_color = theme, shell, surface
        self._primary, self._quiet, self._destructive = primary, quiet, destructive
        self._hovered = self._focused = False
        super().__init__(parent, text=text, command=command, font=font or (app.search_font, 9),
                         relief="flat", bd=0, highlightthickness=0 if shell else dp(1),
                         highlightcolor=theme.input_focus, disabledforeground=theme.text_disabled,
                         padx=dp(5 if compact else 9), pady=dp(1 if compact else 4))
        self.bind("<Enter>", lambda _e: self._track("_hovered", True))
        self.bind("<Leave>", lambda _e: self._track("_hovered", False))
        self.bind("<FocusIn>", lambda _e: self._track("_focused", True))
        self.bind("<FocusOut>", lambda _e: self._track("_focused", False))
        self._paint()

    def configure(self, cnf=None, **options):
        result = super().configure(cnf, **options)
        if "state" in options or isinstance(cnf, dict) and "state" in cnf:
            self._paint()
        return result

    config = configure

    def set_primary(self, active):
        self._primary = active
        self._paint()

    def set_destructive(self, active):
        self._destructive = active
        self._paint()

    def _track(self, attribute, value):
        setattr(self, attribute, value)
        self._paint()

    def _paint(self):
        theme = self._theme
        disabled = str(self.cget("state")) == "disabled"
        if disabled:
            fill = hover = theme.control_disabled
            fg, border = theme.text_disabled, theme.divider
        elif self._destructive:
            fill, hover, fg, border = (theme.danger_soft, theme.event_type_ddl_background,
                                       theme.danger, theme.event_type_ddl_border)
        elif self._primary:
            fill, hover, fg, border = theme.accent, theme.accent_hover, theme.text_on_accent, theme.accent
        else:
            fill = self._surface_color if self._quiet else theme.control_background
            hover, fg = theme.control_hover, theme.text_primary if self._quiet else theme.control_text
            border = fill if self._quiet else theme.control_border
        color = hover if self._hovered and not disabled else fill
        cursor = "" if disabled else "hand2"
        super().configure(bg=color, fg=fg, activebackground=hover, activeforeground=fg, cursor=cursor,
                          takefocus=not disabled, highlightbackground=border)
        if self._shell:
            self._shell.configure(cursor=cursor)
            self._shell.set_fill(color)
            self._shell.set_outline(theme.input_focus if self._focused and not disabled else border)


def form_button(parent, app, text, command, *, primary=False, quiet=False, danger=False,
                destructive=False, font=None, compact=False, radius: int | None = None, rounded=True):
    """Return a native button and the widget to use for geometry management."""
    theme, dp = app.theme, app.dpi.px
    surface = parent.cget("bg")
    shell = (RoundedCardShell(parent, theme, dp, surface, surface=surface,
                              radius=dp(7) if radius is None else radius)
             if rounded and theme.rounded_widget_shells else None)
    button = _FormButton(shell or parent, app, text, command, shell=shell, surface=surface,
                         primary=primary, quiet=quiet, destructive=destructive or danger,
                         font=font, compact=compact)
    if shell:
        button.pack(fill="both", expand=True, padx=dp(5), pady=dp(1 if compact else 2))
    return button, shell or button


def build_segmented_tabs(parent, app, titles, *, padx=0, pady=(0, 0), font=None):
    """Use native Notebook selection and events; draw only its external tab strip."""
    theme, dp = app.theme, app.dpi.px
    bg = parent.cget("bg")
    style = ttk.Style(parent)
    # Remove the native plate as well as the tabs, including under the Vista theme.
    name = "Calendar.Segmented.TNotebook"
    style.layout(name, [("null", {"sticky": "nsew"})])
    style.layout(name + ".Tab", [])
    style.configure(name, padding=0, tabmargins=0, borderwidth=0)
    strip = tk.Frame(parent, bg=bg)
    strip.pack(fill="x", padx=padx, pady=pady)
    tk.Frame(parent, bg=theme.divider, height=dp(1)).pack(fill="x", padx=padx)
    notebook = ttk.Notebook(parent, style=name)
    pages, buttons = [], []
    for index, title in enumerate(titles):
        page = tk.Frame(notebook, bg=bg)
        notebook.add(page, text=title)
        strip.columnconfigure(index, weight=1, uniform="segmented")
        button, outer = form_button(strip, app, title, lambda page=page: notebook.select(page),
                                    quiet=True, font=font)
        outer.grid(row=0, column=index, sticky="ew", padx=(dp(3) if index else 0, 0))
        button.bind("<Return>", lambda _e, button=button: button.invoke())
        pages.append(page)
        buttons.append(button)

    def paint(_event=None):
        selected = notebook.select()
        for page, button in zip(pages, buttons):
            button.set_primary(str(page) == selected)
    notebook.bind("<<NotebookTabChanged>>", paint, add="+")
    notebook.enable_traversal()
    paint()
    return notebook, pages


class _FormCombobox(ttk.Combobox):
    def configure(self, cnf=None, **options):
        result = super().configure(cnf, **options)
        if ("state" in options or isinstance(cnf, dict) and "state" in cnf) and hasattr(self, "_paint_surface"):
            self._paint_surface()
        return result

    config = configure

    def state(self, statespec=None):
        result = super().state(statespec)
        if statespec is not None and hasattr(self, "_paint_surface"):
            self._paint_surface()
        return result


def form_combobox(parent, app, *, font=None, compact=False, **options):
    """Keep native popup, values, keyboard and ComboboxSelected semantics."""
    theme, dp = app.theme, app.dpi.px
    shell = (RoundedCardShell(parent, theme, dp, theme.input_background, outline=theme.input_border,
                              surface=parent.cget("bg"), radius=dp(7), cursor="")
             if theme.rounded_widget_shells else None)
    style = ttk.Style(parent)
    name = f"Calendar.Form.{theme.name}.{dp(16)}.{'Small' if compact else 'Normal'}.TCombobox"
    if "clam" in style.theme_names():
        field, arrow = "CalendarForm.field", "CalendarForm.downarrow"
        elements = style.element_names()
        for element, source in ((field, "Combobox.field"), (arrow, "Combobox.downarrow")):
            if element not in elements:
                style.element_create(element, "from", "clam", source)
        style.layout(name, [(arrow, {"side": "right", "sticky": "ns"}),
                            (field, {"sticky": "nsew", "children": [
                                ("Combobox.padding", {"expand": 1, "sticky": "nsew", "children": [
                                    ("Combobox.textarea", {"sticky": "nsew"})]})]})])
    style.configure(name, padding=(dp(3), dp(1 if compact else 3)), arrowsize=dp(12),
                    arrowcolor=theme.text_secondary, borderwidth=0 if shell else dp(1),
                    foreground=theme.text_primary, background=theme.input_background,
                    fieldbackground=theme.input_background, bordercolor=theme.input_background,
                    lightcolor=theme.input_background, darkcolor=theme.input_background)
    style.map(name, fieldbackground=[("disabled", theme.control_disabled), ("readonly", theme.input_background)],
              background=[("disabled", theme.control_disabled)],
              bordercolor=[("disabled", theme.control_disabled)],
              lightcolor=[("disabled", theme.control_disabled)], darkcolor=[("disabled", theme.control_disabled)],
              foreground=[("disabled", theme.text_disabled)], arrowcolor=[("disabled", theme.text_disabled)],
              selectbackground=[("readonly", theme.accent_soft)], selectforeground=[("readonly", theme.text_primary)])
    combo = _FormCombobox(shell or parent, style=name, font=font or (app.search_font, 9), **options)
    if shell:
        combo.pack(fill="both", expand=True, padx=dp(5), pady=dp(3))
        focused = False

        def paint():
            disabled = combo.instate(("disabled",))
            shell.set_fill(theme.control_disabled if disabled else theme.input_background)
            shell.set_outline(theme.input_focus if focused and not disabled else theme.input_border)

        def focus(active):
            nonlocal focused
            focused = active
            paint()
        combo._paint_surface = paint
        combo.bind("<FocusIn>", lambda _e: focus(True))
        combo.bind("<FocusOut>", lambda _e: focus(False))
        paint()
    return combo, shell or combo


class SettingsScroll(tk.Frame):
    """A growing form with fixed dialog actions and a scrollbar only on overflow."""
    def __init__(self, parent, app):
        bg = app.theme.panel_background
        super().__init__(parent, bg=bg)
        self.canvas = canvas = tk.Canvas(self, width=1, height=1, bg=bg, highlightthickness=0)
        scroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview,
                               style="Global.Vertical.TScrollbar")
        self.content = tk.Frame(canvas, bg=bg)
        item = canvas.create_window(0, 0, anchor="nw", window=self.content)
        canvas.pack(fill="both", expand=True)
        def fit_content(width):
            gutter = app.dpi.px(6) if scroll.winfo_manager() else 0
            canvas.itemconfigure(item, width=max(1, width - gutter))
        canvas.bind("<Configure>", lambda e: fit_content(e.width))
        self.content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox(item)))

        def update_scroll(first, last):
            scroll.set(first, last)
            if float(first) == 0 and float(last) == 1:
                if scroll.winfo_manager():
                    scroll.pack_forget()
            elif not scroll.winfo_manager():
                scroll.pack(side="right", fill="y", before=canvas)
        canvas.configure(yscrollcommand=update_scroll)

        def wheel(event):
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"
        parent.bind("<MouseWheel>", wheel, add="+")

    def reveal(self, widget):
        canvas = self.canvas
        if canvas.winfo_height() <= 1:
            return
        top, height = canvas.canvasy(0), canvas.winfo_height()
        y = widget.winfo_rooty() - self.content.winfo_rooty()
        total = max(1, self.content.winfo_height())
        if y < top:
            canvas.yview_moveto(y / total)
        elif y + widget.winfo_height() > top + height:
            canvas.yview_moveto((y + widget.winfo_height() - height) / total)


def _rounded(master, width, height, radius, inside, edge=None, stroke=1):
    image = tk.PhotoImage(master=master, width=width, height=height)
    for y in range(height):
        for x in range(width):
            dx = max(radius - x - .5, x + .5 - width + radius, 0)
            dy = max(radius - y - .5, y + .5 - height + radius, 0)
            if dx*dx + dy*dy > radius*radius:
                continue
            border = (min(x, y, width-1-x, height-1-y) < stroke
                      or dx*dx + dy*dy > (radius-stroke)**2)
            image.put(edge if edge and border else inside, (x, y))
    return image


def themed_check(parent, theme, dp, **options):
    if not options.get("indicatoron", True):
        return tk.Checkbutton(parent, **options)
    root = parent._root()
    cache = root.__dict__.setdefault("_theme_checks", {})
    size = dp(16)
    key = (theme.name, size)
    if key not in cache:
        off = _rounded(parent, size, size, dp(4), theme.input_background,
                       theme.checkbox_border, dp(1))
        on = _rounded(parent, size, size, dp(4), theme.checkbox_checked)
        for start, end in (((4, 8), (7, 11)), ((7, 11), (12, 5))):
            steps = max(dp(8), 1)
            for i in range(steps+1):
                t = i/steps
                x, y = (dp(round_val) for round_val in
                        (start[0]+t*(end[0]-start[0]), start[1]+t*(end[1]-start[1])))
                on.put(theme.text_on_accent, to=(x, y, x+dp(2), y+dp(2)))
        cache[key] = off, on
    off, on = cache[key]
    options.update(image=off, selectimage=on, compound="left", indicatoron=False,
                   relief="flat", offrelief="flat", bd=0, padx=dp(4),
                   selectcolor=options["bg"], highlightthickness=dp(1),
                   highlightbackground=options["bg"], highlightcolor=theme.input_focus,
                   takefocus=True)
    return tk.Checkbutton(parent, **options)


def configure_scrollbars(root, style, theme, dp):
    """Nine-slice the thumb; leave the whole 10px track available for dragging."""
    cache = root.__dict__.setdefault("_theme_scroll_styles", {})
    for orient in ("Vertical", "Horizontal"):
        base = orient + ".TScrollbar"
        names = [base, "Global."+base]
        if orient == "Vertical":
            names.append("DDL."+base)
        for name in names:
            if name not in cache:
                cache[name] = style.layout(name)
        size = dp(10)
        key = f"Rounded_{theme.name}_{size}.{orient}"
        thumb = key+".thumb"
        # Reuse the toolkit's trough painter. A 1x1 ttk image element tiles
        # across the entire track on every repaint, especially costly at 200%.
        track = cache[base][0][0]
        if key not in cache:
            images = []
            for color in (theme.checkbox_border, theme.text_muted, theme.text_secondary):
                # Transparent margins widen the hit target without widening the ink.
                img = tk.PhotoImage(master=root, width=size, height=size)
                pad = dp(2)
                bit = _rounded(root, size-2*pad, size-2*pad, (size-2*pad)//2, color)
                img.tk.call(str(img), "copy", str(bit), "-to", pad, pad)
                images.append(img)
            style.element_create(thumb, "image", images[0], ("pressed", images[2]),
                                 ("active", images[1]), border=(size-1)//2, sticky="nswe")
            cache[key] = tuple(images)
        layout = [(track, {"sticky": "nswe", "children": [
            (thumb, {"expand": "1", "sticky": "nswe"})]})]
        for name in names:
            style.layout(name, layout)

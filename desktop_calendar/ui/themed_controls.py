"""Shared rounded control artwork. Native widgets retain their class bindings."""
import tkinter as tk
from tkinter import ttk
import math


# A superellipse quadrant (exponent 3) blends into the straight edges with
# vanishing curvature. Sample once, then reflect the same corner four ways.
_ARC = tuple((math.cos(i*math.pi/24)**(2/3), math.sin(i*math.pi/24)**(2/3))
             for i in range(13))


class RoundedCardShell(tk.Canvas):
    def __init__(self, parent, theme, dp, fill, *, outline=None, cursor="hand2", surface=None):
        super().__init__(parent, bd=0, highlightthickness=0,
                         bg=surface or theme.schedule_background, cursor=cursor)
        self._radius = dp(10)
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


def form_entry(parent, app, variable, *, show="", width=1, rounded=True):
    """The geometry widget may be a shell; the entry remains a native Tk field."""
    theme, dp = app.theme, app.dpi.px
    shell = (RoundedCardShell(parent, theme, dp, theme.input_background, surface=parent.cget("bg"),
                              outline=theme.input_border, cursor="xterm")
             if rounded and theme.rounded_widget_shells else None)
    entry = tk.Entry(shell or parent, textvariable=variable, show=show, width=width,
                     font=(app.search_font, 9), bg=theme.input_background,
                     fg=theme.text_primary, insertbackground=theme.text_primary,
                     relief="flat", bd=0, highlightthickness=0 if shell else dp(1),
                     highlightbackground=theme.input_border, highlightcolor=theme.input_focus)
    if shell:
        entry.pack(fill="both", expand=True, padx=dp(8), pady=dp(5))
        shell.bind("<Button-1>", lambda _e: entry.focus_set())
        entry.bind("<FocusIn>", lambda _e: shell.set_outline(theme.input_focus))
        entry.bind("<FocusOut>", lambda _e: shell.set_outline(theme.input_border))
    return entry, shell or entry


def form_button(parent, app, text, command, *, primary=False, quiet=False):
    """A native button with a theme surface, for settings actions that stay enabled."""
    theme, dp = app.theme, app.dpi.px
    surface = parent.cget("bg")
    fill = theme.accent if primary else surface if quiet else theme.control_background
    hover = theme.accent_hover if primary else theme.control_hover
    fg = theme.text_on_accent if primary else theme.control_text
    border = theme.accent if primary else fill if quiet else theme.control_border
    shell = (RoundedCardShell(parent, theme, dp, fill, surface=surface, outline=border)
             if theme.rounded_widget_shells else None)
    button = tk.Button(shell or parent, text=text, command=command, font=(app.search_font, 9),
                       bg=fill, fg=fg, activebackground=hover, activeforeground=fg,
                       relief="flat", bd=0, highlightthickness=0 if shell else dp(1),
                       highlightbackground=border, highlightcolor=theme.input_focus,
                       cursor="hand2", padx=dp(9), pady=dp(4))
    if shell:
        button.pack(fill="both", expand=True, padx=dp(5), pady=dp(2))
    def paint(_event=None, hovered=None):
        if hovered is not None:
            paint.hovered = hovered
        color = hover if paint.hovered else fill
        button.configure(bg=color)
        if shell:
            shell.set_fill(color)
            shell.set_outline(theme.input_focus if button.focus_get() == button else border)
    paint.hovered = False
    button.bind("<Enter>", lambda e: paint(e, True))
    button.bind("<Leave>", lambda e: paint(e, False))
    button.bind("<FocusIn>", paint)
    button.bind("<FocusOut>", paint)
    return button, shell or button


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
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
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

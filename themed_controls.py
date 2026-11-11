"""Shared rounded control artwork. Native widgets retain their class bindings."""
import tkinter as tk
import math


# A superellipse quadrant (exponent 3) blends into the straight edges with
# vanishing curvature. Sample once, then reflect the same corner four ways.
_ARC = tuple((math.cos(i*math.pi/24)**(2/3), math.sin(i*math.pi/24)**(2/3))
             for i in range(13))


class RoundedCardShell(tk.Canvas):
    def __init__(self, parent, theme, dp, fill, *, outline=None, cursor="hand2"):
        super().__init__(parent, bd=0, highlightthickness=0,
                         bg=theme.schedule_background, cursor=cursor)
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

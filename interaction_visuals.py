"""Small presentation helpers; no event mutations, calendar rules or storage."""

import tkinter as tk
from dataclasses import dataclass

from ui_draw import blend


def visible_rows(count, top, height, row_height, overscan=3):
    if row_height <= 0:
        return range(0)
    first = max(0, min(count, int(max(0, top) // row_height) - overscan))
    last = min(count, int((max(0, top) + max(0, height)) // row_height) + overscan + 1)
    return range(first, max(first, last))


def preview_bounds(x, y, width, height, viewport, padding):
    left, top, right, bottom = viewport
    width = min(width, max(1, right - left - padding * 2))
    height = min(height, max(1, bottom - top - padding * 2))
    if y + height > bottom - padding:
        y -= height + padding * 2
    x = min(max(x, left + padding), right - width - padding)
    y = min(max(y, top + padding), bottom - height - padding)
    return x, y, x + width, y + height


def match_spans(text, query):
    """Highlight only; matching/ranking remains in event_search unchanged."""
    folded, source = "", []
    for index, character in enumerate(text):
        part = character.casefold()
        folded += part
        source.extend([index] * len(part))
    matches = []
    for token in set(query.casefold().split()):
        offset = 0
        while token and (found := folded.find(token, offset)) >= 0:
            matches.append((source[found], source[found + len(token) - 1] + 1))
            offset = found + len(token)
    merged = []
    for start, end in sorted(matches):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def elide(text, font, width):
    text = " ".join(text.split())
    if width <= 0:
        return ""
    if font.measure(text) <= width:
        return text
    left, right = 0, len(text)
    while left < right:
        middle = (left + right + 1) // 2
        if font.measure(text[:middle] + "…") <= width:
            left = middle
        else:
            right = middle - 1
    return text[:left] + "…" if font.measure("…") <= width else ""


class CanvasLayer:
    """Tag a drawing pass and replace callbacks without leaking Tcl commands.

    Existing renderer calls and rounded helpers can keep using Canvas methods.
    Only visible row items are recreated; static date layers remain intact.
    """

    def __init__(self, canvas, tag=""):
        self.canvas, self.tag = canvas, tag
        if not hasattr(canvas, "_interaction_bindings"):
            canvas._interaction_bindings = {}

    def __getattr__(self, name):
        original = getattr(self.canvas, name)
        if name.startswith("create_") and self.tag:
            def create(*args, **kwargs):
                tags = kwargs.get("tags", ())
                kwargs["tags"] = ((tags,) if isinstance(tags, str) else tuple(tags)) + (self.tag,)
                return original(*args, **kwargs)
            return create
        return original

    def tag_bind(self, tag, sequence, callback):
        registry = self.canvas._interaction_bindings
        key = tag, sequence
        if key in registry:
            registry[key][1] = callback
        else:
            record = [None, callback]
            record[0] = self.canvas.tag_bind(tag, sequence, lambda event: record[1](event))
            registry[key] = record

    def prune_bindings(self):
        registry = self.canvas._interaction_bindings
        for (tag, sequence), (identifier, _callback) in tuple(registry.items()):
            if not self.canvas.find_withtag(tag):
                self.canvas.tag_unbind(tag, sequence, identifier)
                del registry[tag, sequence]


class CanvasOverlay:
    """Bounded item pool for pointer-rate previews; hide unused slots."""

    def __init__(self, canvas, tag):
        self.canvas, self.tag, self.items, self.used = canvas, tag, {}, set()

    def begin(self):
        self.used.clear()

    def item(self, key, kind, coordinates, **options):
        identifier = self.items.get(key)
        if identifier is None or not self.canvas.type(identifier):
            identifier = getattr(self.canvas, "create_" + kind)(*coordinates, tags=self.tag, **options)
            self.items[key] = identifier
        else:
            self.canvas.coords(identifier, *coordinates)
            self.canvas.itemconfigure(identifier, state="normal", **options)
        self.used.add(key)
        return identifier

    def end(self):
        for key in self.items.keys() - self.used:
            self.canvas.itemconfigure(self.items[key], state="hidden")
        self.canvas.tag_raise(self.tag)

    def clear(self):
        self.used.clear()
        self.end()


@dataclass(frozen=True)
class PaletteRow:
    top: int
    height: int
    index: int | None
    label: str = ""


def palette_rows(results, scale):
    rows, top, previous = [], 0, None
    for index, result in enumerate(results):
        section = "匹配事项" if result.kind == "event" else "快捷操作"
        if previous != section:
            rows.append(PaletteRow(top, scale(28), None, section))
            top += scale(28)
            previous = section
        height = scale(58 if result.kind == "event" else 44)
        rows.append(PaletteRow(top, height, index))
        top += height
    return tuple(rows), top


class InteractionFeedbackMixin:
    def _clear_feedback(self):
        job = getattr(self, "_feedback_job", None)
        if job:
            self.after_cancel(job)
        self._feedback_job = None
        label = getattr(self, "_feedback_label", None)
        if label is not None and label.winfo_exists():
            label.destroy()
        self._feedback_label = None

    def _show_feedback(self, text, *, success=True):
        self._clear_feedback()
        parent = getattr(self, "global_status_label" if self.view_mode == "global" else "footer_frame", None)
        if parent is None or not parent.winfo_exists():
            return
        theme = self.theme
        self._feedback_label = tk.Label(parent, text=text, anchor="w", padx=self.dpi.px(10),
                                       font=(self.search_font, 8), bg=blend(theme.panel_background, theme.accent_soft, .24),
                                       fg=theme.quick_success if success else theme.text_secondary)
        self._feedback_label.place(x=0, y=0, relwidth=1, relheight=1)
        self._feedback_job = self.after(2200, self._clear_feedback)

    def _flash_event(self, event_id):
        if self.view_mode != "global":
            return
        canvas = self.global_flow_canvas if self.global_display_mode == "flow" else self.global_timeline_canvas
        previous = getattr(self, "_flash_job", None)
        if previous:
            self.after_cancel(previous)
        canvas.delete("interaction-flash")
        for hit in self._schedule_hits:
            if hit.event_id == event_id:
                canvas.create_rectangle(*hit.bounds, outline=self.theme.accent, width=self.dpi.px(2),
                                        dash=(self.dpi.px(3), self.dpi.px(2)), tags="interaction-flash")
        self._flash_job = self.after(1300, lambda: self._end_event_flash(canvas))

    def _end_event_flash(self, canvas):
        self._flash_job = None
        if canvas.winfo_exists():
            canvas.delete("interaction-flash")

# v0.9.0 interaction engine, phase 1

## Existing architecture and scope

The v0.8.2 baseline has a 7,226-line `app.py`: Tk widgets, Canvas renderers,
modal lifecycle, event callbacks, reminders and application coordination share
that module. `calendar_core.Store` owns JSON persistence and the date/workday
rules. Each multi-day event is one canonical `Event.id`; both Global renderers
already consume the immutable Timeline model. No series migration is needed.

Previously most dialogs called CalendarApp's event methods, while quick add,
postpone and notification actions saved directly. Completion/postpone mutated
the canonical Event before Store could compare the old schedule. Every event
action called the general render path, and the editor could refresh the same
day dialog again when closing. Category changes also saved twice.

Phase 1 extracts only event commands. The Task schema, Timeline model, Flow
layout, themes, DPI, window ownership and update logic are unchanged.

## Public API

`EventCommandHistory(store, limit=100)` owns the in-memory undo/redo stacks
(minimum 50 entries). `upsert(event)`, `update(id, **fields)`, `delete(id)` and
`create_quick(title, day, **options)` return an `EventChange` after persistence
succeeds. `undo()` and `redo()` use the same persistence and change result.
No-op edits neither save nor discard redo. A new successful command clears redo.

Only the affected Event's scalar fields, list position and notification keys
are kept per command. No settings, categories, habits or database snapshots are
stored in history. `Store.persist_event_state()` stages a replacement list and
rolls memory back on failure; JSON keeps its existing temporary-file + replace
commit. Notification pruning is published in memory after the file commit.

## Future drag contract

1. Keep the canonical event ID on pointer-down; do not mutate Store.
2. On pointer-move call `preview_schedule(id, start_date=..., duration_days=...)`
   or `preview_schedule(id, start_date=..., end_date=...)`.
3. Render `EventPreview.effective_dates`, `deadline`, `effective_color`, and
   `preview.event()` as a separate transient overlay. Rendering a preview does
   not require insertion into Store or rebuilding persisted models.
4. On Esc, discard the preview. On mouse release call
   `CalendarApp.commit_event_preview(preview)` once; it commits, persists and
   refreshes the resulting `EventChange` through the shared coordinator.

`preview_update` and `preview_upsert` support other candidate fields. Previews
are detached and contain immutable scalar snapshots. Commit rejects a stale
task, changed inherited category color, or changed effective date expansion.
Both Global renderers use exactly the same interface.

Start-only moves preserve the effective-day duration and time of day. Resizing
counts effective days using Store's existing `is_workday` (including adjusted
work weekends and custom leave). A non-working requested end resolves to the
last valid day within the range; an empty range is rejected. Native DDL and
end-as-DDL use the resulting last effective date, with task type and completion
preserved. The renderer remains responsible for clipping and week splitting.

## UI and limitations

Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z are routed for all app windows; text widgets retain
their editing keys. Event notes enable Tk Text undo. An open editor blocks
background event-history shortcuts so unsaved fields cannot overwrite undo.
The menu exposes undo/redo and their last operation names.

Compact updates affected date cells and necessary agenda/DDL sections. Global
updates the active month model and Canvas when affected; weekly lane packing
and Timeline ordering require that Canvas redraw. The shell, Sidebar, theme
and modal windows are not rebuilt. Related open day/DDL windows refresh once.

V1 does not undo habit management, category catalog CRUD, custom leave settings,
window/theme/filter state, or automatic reminder delivery. Catalog changes clear
event history because they can delete category links or change many inherited
colors. Untracked changes to a task cause a conflict instead of silent overwrite.
Undo/redo failures preserve the stacks and data. History is not saved and is
empty after every restart. Nothing is pushed, merged or released by this phase.

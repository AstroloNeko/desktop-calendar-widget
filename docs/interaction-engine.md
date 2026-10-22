# v0.9.0 interaction engine

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

## Phase 2: shared scheduling gestures

`schedule_drag.ScheduleDrag` owns the immutable source snapshot, threshold,
operation (`move`, `start`, `end`), validation and final `EventPreview`.
`GlobalDragMixin` only translates Canvas coordinates, manages grabs/cancellation
and draws a small overlay at most once per 16 ms. Both renderers register
`ScheduleHit` rectangles; only actual first/last effective dates have resize
handles, including single-day events. Clipped or intermediate weekly pieces
do not invent endpoints.

Movement preserves the offset of the grabbed day relative to the original
start. Resize uses `EventCommandHistory.preview_schedule` and Store's effective
date rules. No Store writes occur before release. Release outside, Esc, modal
opening, theme/view changes, external event edits and focus loss cancel safely.
Horizontal edge dwell scrolls Timeline first, then changes month; Flow changes
month after a 750 ms dwell. PgUp/PgDn also navigate while retaining the gesture.
Only deliberate month navigation redraws the complete renderer during a drag.

## Global search and command palette

`EventSearchIndex` projects all canonical events (past/future/completed included)
into immutable search documents. It reuses Store's actual effective dates,
deadline and category color. A detached snapshot feeds one background worker;
no worker touches Tk, saves data, or mutates a canonical Event. Title exact,
prefix, substring, then category/notes matches rank by date and stable ID.
Case folding and whitespace normalization apply to Chinese/English queries.

`EventSearchService` caches the index by revision, cancels queued stale requests
and delivers only the latest ticket via a queue. Event commands (including
undo/redo), category changes and date-state changes invalidate it. UI input is
debounced 70 ms; results are capped at 100 events plus matching commands.
Narrow the query to find additional matches. History/indexes are session-only.

Ctrl+K is registered once and shared by Compact/Flow/Timeline. SearchDialog
uses the existing modal owner stack, releases its grab before an action opens
an editor, restores focus on Escape, and never steals an existing editor's
unsaved session. Seven commands reuse existing application entry points.
Explicit year-month-day date formats are accepted; ambiguous natural dates are
not interpreted. Searching itself creates no undo records.

Activating an event selects its actual start date and opens the existing editor.
Global also selects/scrolls the result and temporarily reveals that stable ID,
not its entire category. Both renderers consume the same revealed month model.
The Sidebar selection is never saved/changed by search; the next filter action
clears temporary reveal. A summary hint identifies the temporary state. Flow
overflow events remain accessible through the editor even below visible lanes.

## Validation and performance

`py -3 tools/check_interactions.py --performance` opens real native test windows
using only a temporary Store. It checks four themes, Compact/Flow/Timeline,
move/start/end resize and undo, cancellation, search singleton, focus/grabs,
keyboard activation and filtered results. It also records 100/500/1000-event
search, Flow scroll, category redraw and Timeline preview costs. It never
registers a tray icon or sends real reminders. Headless tests separately cover
96/120/144/168/192 DPI coordinate mapping and thresholds.

At actual 192 DPI on the development machine, the 1000-event sample took
approximately 14 ms to build its background index, 2 ms per search, 10–11 ms
per drag preview frame, 21–25 ms per actual Flow scroll paint, and 125–135 ms
for a category-triggered full redraw.
Filtering and committed mutations still require one lane/row-layout redraw;
motion does not. These are local observations, not cross-machine guarantees.

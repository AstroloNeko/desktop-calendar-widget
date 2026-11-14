# AI Habit 2.0 creation

This is a scoped addition to Calendar Chat, not a second recurring Event system.
The GUI constructs `RoutineItem(kind='habit')` using `validate_schedule_input`
and persists through `Store.upsert_routine`. The new gateway uses the same strict
validator, model and Store method. `habit_schedule.py`, Event models, calendar
schema, existing completion history and reminder date rules are unchanged.

## Tool and mapping

`preview_create_habit` takes title/frequency plus optional interval, weekdays,
month_day, year_month, start_on, end_on, workdays_only, color, reminder_enabled
and reminder_time. Arrays are strictly typed; weekday values are Monday=0 through
Sunday=6. Daily/weekly/monthly/yearly and interval 1–999 map directly to existing
Habit fields. Missing start is local today; missing color is the editor's mint
green, no reminder, no end and not restricted to workdays. IDs and completion
history are not model-writable. This tool cannot create one-shot todo items.

Explicit numeric intervals, weekday phrases and relative start phrases are checked
locally. Monday selection is independent of the start date; next planned date is
observed using `Store.routine_planned_on`, including holiday/leave filtering.
Monthly 29/30/31 and yearly February 29 use the existing skip-missing-date rule,
never clamping or drifting after a missed check-in. End date is inclusive. The
existing model has no dynamic last-day-of-month or repeat-count ending: clarify.
Other natural date/title interpretation is still performed by the model and
validated, not a general deterministic language parser.

Colors resolve from the actual COLORS palette (or explicitly supplied user HEX),
not Event category inheritance. Unsolicited model reminder/start/end/color defaults
are ignored. Reminder enablement requires explicit user intent and a clock phrase;
missing time clarifies even if the model invents a syntactically valid HH:MM.
Explicit no-reminder takes precedence over model defaults.

## Execution and identity

- Event and Habit proposals share whole-response preflight: more than one write
  proposal is rejected before any preparation/execution, including mixed kinds.
- A valid single creation follows the existing automatic host policy, protected
  journal intent/result receipts, authenticated owner/request IDs and Tk main-thread
  dispatcher. A request replay never repeats the write; a different request can
  intentionally create another habit with the same title. Never deduplicate titles.
- Habit saves use Store's regular persistence and backups. A save exception rolls
  back the newly inserted in-memory item; only persisted success returns success.
  Durable executing receipts reconcile by stable Habit ID/full state, never replay
  automatically after a crash. A successful save is not replayed if UI refresh fails.
- Session identity records selected_kind and stable ID. Provider context contains
  typed selected_object; the Event-ID context is empty for a Habit. Follow-up habit
  edits/completion/deletion are explicitly unsupported, never passed to Event tools.
- Source UI and HTTP use the same service. UI result is a concise habit title and
  schedule, not the Event change table. Main-thread refresh invalidates reminders,
  redraws the current view and refreshes an open habit manager without restart.

## Limitations and checks

The existing command history supports Events only. This phase does not fabricate a
separate Habit Undo history; created habits can be edited/deleted in Habit Manager.
AI Habit modification/deletion/check-in is out of scope. Next-occurrence display
searches at most eight years and may be null for long intervals; this is only a
display hint and never limits persistence or scheduler recurrence.

All automated/GUI/live tests use temporary Stores and journals. Live smoke uses
existing consented credentials read-only, two fictional habit requests (weekly
Monday/purple and every-three-days PCR); both succeeded in two model requests.
This is finite model evidence, not proof of all natural-language phrasings.
QA EXEs are built only under ignored build/, not release/. The physical Windows
display is 192 DPI (200%); no claim of five physical DPI environments.

Verification total: 898 tests (878 baseline + 20 new habit-focused tests with
parameterized cases). Source and frozen QA each pass eight theme/view cases,
including real Habit persistence, concise UI result, modal cleanup and loopback
HTTP. Event Undo smoke remains Event-only, not a claim of Habit Undo support.

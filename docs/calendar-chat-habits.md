# AI Habit 2.0 creation

This is a scoped addition to Calendar Chat, not a second recurring Event system.
The GUI constructs `RoutineItem(kind='habit')` using `validate_schedule_input`
and persists through Store. GUI and AI now share RoutineCommand on the existing
EventCommandHistory stack and Store persistence. The matching recurrence algorithm
and Event model remain unchanged; Schema 9 adds historical rule snapshots.

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
  typed selected_object; the Event-ID context is empty for a Habit. Query/detail
  tools establish real candidates, and edits require a selected Habit ID. Multiple
  same-name candidates require UI/number selection, never the model's first guess.
- Source UI and HTTP use the same service. UI result is a concise habit title and
  schedule, not the Event change table. Main-thread refresh invalidates reminders,
  redraws the current view and refreshes an open habit manager without restart.

## Limitations and checks

AI Habit deletion/check-in and conversion to one-shot todo remain out of scope.
Next-occurrence display
searches at most eight years and may be null for long intervals; this is only a
display hint and never limits persistence or scheduler recurrence.

All automated/GUI/live tests use temporary Stores and journals. Live smoke uses
existing consented credentials read-only, two fictional habit requests (weekly
Monday/purple and every-three-days PCR); both succeeded in two model requests.
This is finite model evidence, not proof of all natural-language phrasings.
QA EXEs are built only under ignored build/, not release/. The physical Windows
display is 192 DPI (200%); no claim of five physical DPI environments.

The initial creation baseline passed 898 tests. The editing extension additionally
checks scoped patches, temporal stats, conflict/duplicate/concurrent requests,
backup/restart, migration, unified Undo/Redo, four-theme GUI and authenticated HTTP.

## Editing extension and compatibility

- New tools: query_habits, get_habit, preview_update_habit(habit_id, changes).
  Query results exclude raw check-in/history arrays. Detail reads expose editable
  attributes, not private historical records. One user edit is one atomic command.
- Only requested fields survive the patch scope. Editing color cannot reset title,
  reminder or dates. Weekly changes cannot contaminate dormant month/year fields.
  Reminder-off keeps the existing clock; explicit clock edits enable reminders.
  Year-end/endless and next-month starts are mapped to the same model fields.
- A rule change takes effect on the local calendar date of the change. Earlier
  dates choose archived rules; matching is still habit_scheduled_on plus Store's
  workday calendar. Multiple changes on the same date retain only the original
  before-date snapshot. Existing habit_done and completed_on records are untouched.
  This preserves past planned-date/statistics denominators, not just check-ins.
  Rules before this feature existed cannot be reconstructed: migration treats the
  previously persisted rule as historical truth. Changes to holiday/leave calendar
  data itself still follow Store's live calendar; no holiday-version system added.
- schedule_history is per Habit, not a second recurring task model. Schema 8 and
  earlier load with empty history; existing migration checkpoints preserve original
  bytes before the first Schema 9 save. Backup container version is unchanged.
  Old Schema-8 programs safely refuse Schema 9 instead of silently dropping rule
  archives. Invalid archives fail loading/saving closed. Test development versions
  with isolated data; an old released EXE cannot read the new format.
- RoutineCommand uses the existing session-local global undo/redo stack. Rule,
  attributes and rule archives restore atomically; check-ins made after the command
  survive undo/redo. A changed/deleted target conflicts rather than overwriting.
  Undo of creation is blocked when check-in records already exist. GUI edits use
  this same history, not a parallel AI stack; reminder/UI refresh works on undo.
- Explicit edit requests that stop after a model read get at most the existing
  bounded repair budget, never a fabricated write result. The first live run (6
  calls) stopped after reading an already-green target; the second used the purple
  target and passed creation plus weekday/color/workday/reminder-time/off followups
  in 11 calls, all synthetic. These finite samples do not establish universal NL
  accuracy; ordinary ambiguity still clarifies.

Editing verification: 915 total automated tests (898 creation baseline + 17 new
editing tests with subcases), Python compile and diff checks passed. Source and
frozen QA each passed eight four-theme × Compact/Global cases at physical 192 DPI,
including consecutive edits, same-stack Undo/Redo, reminder invalidation, modal
cleanup and authenticated HTTP edits. No production profile or Release was used.

# Calendar AI property adaptation

The existing Event, Store, EventCommandHistory, Tk dispatcher and local HTTP
authentication remain the only persistence path. No calendar schema change.

## GUI contract audit

| GUI field | AI field / legal values | New-event default / constraints |
|---|---|---|
| Title | title, nonempty string, AI cap 300 | Required; preserve explicit names |
| Start date | date, ISO YYYY-MM-DD, 1900–2199 | Required; local runtime date context |
| Optional time | time, HH:MM or null | No time; internal 23:59, not a fabricated appointment time |
| Duration | duration_days, integer 1–365 | 1; Store counts actual effective days |
| Skip nonworking | skip_non_working_days, boolean | false; Store respects holidays, compensatory workdays and custom leave |
| Last-day deadline | end_as_ddl, boolean | false; only multi-day general/urgent |
| Nature | event_type: general/urgent/ddl | general; importance alone does not imply urgent |
| Category | category_id, existing ID or null | null unless explicit or high-confidence creation inference |
| Individual color | color, #RRGGBB | Current Store palette default; GUI supports custom HEX |
| Color source | color_mode: inherit/override | Without category always override; new category selection inherits unless explicitly overridden |
| Reminder | reminder, null or 0–10080 minutes | None; requires concrete time. GUI presets: none, 0,10,30,60,1440,4320,10080; business supports intermediate minute values |
| Notes | notes, string, AI cap 2000 | Empty; contents cannot authorize extra field edits |
| Completed | preview_complete(done: boolean) | false; separate completion action |

IDs, created_at and snooze_until are not model-writable. Title/date/time/notes
changes use detached partial patches, preserving all other existing Event fields.

## Adaptation and safety

- Runtime category catalog contains real IDs/names/colors and the current GUI
  palette. It is refreshed each request. Only the selected target's detailed
  read includes notes; search summaries omit notes. Catalog text is untrusted data.
- New tool fields: end_date (inclusive date range) and category_basis
  (explicit/semantic_high). The latter is proposal metadata, never an Event field.
  Semantic inference is allowed only for creation and only with a valid real ID.
  It is model confidence, not a calibrated statistical score. Explicit category
  names or no-category override inference; ambiguous matching names clarify.
- Named colors resolve locally from COLORS, not model-generated HEX. Explicit
  custom HEX remains supported. Creation with category and no explicit color
  inherits. Category-only edits preserve existing overrides; explicit follow
  switches to inheritance. The dormant Event.color value is retained, as in the
  existing model; effective_event_color ignores it while inheriting.
- end_date uses Store.is_workday and Store.event_dates, not a separate holiday
  algorithm. A nonworking end date with skip enabled clarifies instead of moving
  silently. Range is capped at 1095 calendar days and 365 effective days.
- Month/day without year means the local current year; a past month/day
  clarifies rather than silently jumping to next year. Other relative date/time
  interpretation uses runtime context and the model; ambiguity must clarify.
- Field provenance separates explicit, inferred, defaulted and ambiguous scope.
  Scope detection is conservative, not a universal natural-language parser.
  Notes and rename targets containing color/category words cannot authorize
  unrelated changes. Global preference requests do not create hidden rules.
- Actual repository baseline had no automatic execution policy or delete tool.
  The service now defaults to automatic single-event create/update/complete via
  the existing journal, conflict checks and commands. The same policy is used by
  desktop and HTTP. Explicit “先预览/不要执行/先确认” preserves a pending preview.
  auto_execute=False retains confirmation mode for integrations/tests. This is
  a host policy, never an LLM argument. Authorization is recorded separately
  from user-confirmed_at. Multiple proposals are rejected before any mutation.
- preview_delete only prepares a user-confirmed deletion. It uses command delete,
  Undo preserves original ID and fields; repeated confirmation never deletes twice.
  Pending deletion blocks other operations until confirmed or cancelled.

## Verification

All write tests use temporary Stores and journals. The opt-in live checker
`tools/check_calendar_chat_live.py --live --attributes --report <ignored-path>`
uses fictional categories/events and existing consented credentials read-only.
Two real Chinese combinations passed in 3 DeepSeek requests: WB + purple override
and 15:00 meeting + 30-minute reminder + inherited meeting category. Those samples
do not prove arbitrary model language accuracy. No real user calendar is opened.

Source and frozen QA use `tools/check_calendar_chat.py`, exercising four themes,
Compact/Global, explicit preview/confirmation, invalid multi-proposal preservation,
Undo, modal cleanup, search and authenticated HTTP automatic result/idempotency.
Current physical DPI is 192 (200%), not five physical DPI environments. QA builds
stay under ignored build/; no release artifact, version or tag is changed.

Final automated suite: 878 tests (857 prior + 21 property/policy regressions).
Legacy confirmation-mode tests explicitly construct auto_execute=False; new
policy tests and desktop/HTTP QA exercise the default automatic policy.

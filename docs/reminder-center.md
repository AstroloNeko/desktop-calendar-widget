# Background and reminder center

Branch: `codex/v0100-reminder-center`, based on native reminder commit `2972558`.
Product version stays 0.9.3. No installer, autostart changes, version/schema bump,
new dependency, Release, main merge or push.

## User entry points

- More → **后台与提醒…**: existing global opt-in (still off by default), clock,
  general/DDL lead days, privacy and per-event rules, plus a synthetic test button.
  The page explains tray restore/true exit, current close policy, unchanged Global
  taskbar minimize, and where to configure independent Habit reminders.
- **保存并查看计划** validates/saves all current settings before changing views.
  Invalid input keeps the settings window and its input intact.
- More → **提醒计划…**: bounded scrollable window, grouped into needs attention,
  snoozed and upcoming. Select a row for effective date, original planned time,
  earliest next attempt and reason. Refresh is read-only. Open/double-click closes
  this list and uses the existing search-reveal/editor route; no second CRUD.
- Cancel Snooze uses the existing engine API; dates/completion are unchanged and
  the original notification is not replayed. Legacy Snooze can also be cancelled
  before the first scheduling tick, with one atomic state save.

Instructions are inline, not a new startup modal. No new onboarding flag or
changed lifecycle preference is written. Closing settings without saving discards
unsaved preference edits; the test action does not save or enable them.

## Read-only model and refresh

`ReminderEngine.overview(now)` returns a detached `ReminderOverview`/`ReminderRow`
projection from existing config/state and `plans_for_store`. It does not call
`tick`/`_rebuild`, save/import state, send/remove notifications or mutate the heap.
Effective dates, skip days, native/end-as-DDL rules and legacy import interpretation
are reused. Dispatch and projection share `snoozed_plan`, not two date algorithms.

The view reflects the existing retry/catchup/burst rules, without changing them:
failed/exhausted/expired or invalid Snooze need attention; submitted plans are
counted separately, never shown as future sends or claimed seen/read. Already
completed/deleted/disabled events and superseded/cancelled plans are excluded.
Stable within-group ordering is by earliest attempt and scheduled time.

An ephemeral revision increments only on successful engine persistence. Existing
UI polling refreshes an open plan window on invalidation/revision change, or at
most once per 15 seconds for time-derived status. Closed views do not scan plans.
There is no extra timer/thread. Group expansion, selection and scroll position
survive refresh when possible. Errors shown by the service are explicitly its
latest status, not a durable per-event diagnosis.

## Synthetic test

The explicit button queues a `test` job on the **same** WinRT backend thread and
permission/registration path as event notifications. The payload contains no event
title, ID, notes, date, Snooze actions or private data. It uses a separate random
test tag/payload and `test_delivery`/`test_error` results, never event ledger ACKs.
A matching click only confirms the test; it cannot open/create a calendar event.

Test can be requested while event reminders are off; it does not opt in. Pending
clicks are coalesced, pending result wait is bounded to 15 seconds and a temporary
60-second backend lease allows callbacks before normal disabled-service cleanup.
Existing true-exit cleanup still silences/stops it. No worker calls Tk. Windows
acceptance is **not** a visibility/read receipt. Failure/unknown/timeout text does
not promise that a banner was seen. Late acceptance cannot overwrite a confirmed
test click.

## Verification

- Automated: 746/746 passed (704 baseline + 42 new tests); compile/diff checks.
  Coverage includes read-only bytes/cache invariants, legacy state, effective DDL
  dates, no event CRUD/Undo effects, cancellation before first tick, retries,
  burst gates, sorting, future-state rejection, test worker/results isolation,
  timeout/late callback, backend lease, view refresh coalescing.
- Source real Windows/Tk: Modern, Windows 7 Glass, Paper, Frutiger × Compact/Global
  × pinned off/on (16 cases), physical 192 DPI. Both windows scroll; list final row
  reachable, fixed actions visible, settings→plans→editor uses correct owner/grab,
  cancel refreshes, the shared Ctrl+K search entry can reopen after close (callback
  level, not an external physical-keyboard test), data and Undo unchanged. Also a
  simulated 1280×800 work area at physical 192 DPI, not a physical monitor change.
- Existing reminder UI smoke: 16 cases including modal/pinned/search and queued
  synthetic hidden reveal/Snooze passed. Native delivery is separate from mock UI.
- Real synthetic test through the actual settings button, global reminders off:
  direct automation launch was accepted but user saw no notification. Retrying via
  interactive Explorer launch was accepted, **user saw/clicked it**, real click
  callback received. No data bytes/ledger changed, no orphan grab. This records the
  launch-context difference; it does not establish a single proven OS root cause.
  Local ignored reports: `build/reminder-center/native-test.json` and
  `build/reminder-center/interactive-test.json`.

No new packaged EXE was built in this UI iteration. Native EXE Snooze button,
standard-user-token execution, physical sleep/wake/DST and Windows Focus/permission
toggles remain manual release regression items from Phase 1B. Automated DPI
96/120/144/168/192 logic tests are not five physical monitor tests. Notifications
after complete exit/cold activation are still unsupported. This is not a full
historical notification receipt center or a native Habit scheduler.

Developer check (temporary Store, fake native service by default):

```powershell
& 'E:\Software\Python\Python313\python.exe' tools/check_reminder_center.py
```

Opt-in **one real synthetic notification**, isolated data/owned registration,
90-second bounded observation, report must stay in this repo's ignored build/:

```powershell
& 'E:\Software\Python\Python313\python.exe' tools/check_reminder_center.py --native-test
```

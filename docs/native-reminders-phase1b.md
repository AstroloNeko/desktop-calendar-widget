# Phase 1B: Windows native event reminders

Branch: `feature/v0100-native-reminders`, based on Phase 1A `3d56347`.
Product version stays 0.9.3; no Release/tag/main merge in this phase.

## Backend and technical gate

Windows App SDK AppNotificationManager was investigated first. It would add SDK,
runtime/bootstrap and redistribution requirements to the current ZIP product.
The implemented path uses the in-box Windows.UI.Notifications API with a small
C ABI bridge compiled by the existing MSVC/Windows SDK (C++20, x64, static CRT).
No Python package or new tool was installed; no WinUI/.NET migration.

References:

- https://learn.microsoft.com/en-us/windows/apps/develop/notifications/
- https://learn.microsoft.com/en-us/windows/win32/shell/quickstart-sending-desktop-toast
- https://learn.microsoft.com/en-us/windows/win32/shell/enable-desktop-toast-with-appusermodelid
- https://learn.microsoft.com/en-us/windows/win32/api/notificationactivationcallback/nn-notificationactivationcallback-inotificationactivationcallback
- https://github.com/microsoft/WindowsAppSDK/blob/main/dev/AppNotifications/AppNotificationUtility.cpp

The initial prototype reported Show acceptance/history without a visible banner;
this was **not** a pass. Its COM registration was incomplete. QA now installs an
owned AUMID + shortcut + activator CLSID/LocalServer32 and a running COM factory.
Tests launched through the interactive Explorer session established real display,
correct name/icon (user confirmed), real activation, queued Tk processing, and
hidden-tray restoration in source and PyInstaller QA EXE. No causal claim is made
that one registration/launch-context change alone explains every initial failure.

This machine has UAC disabled (`EnableLUA=0`); even Explorer-launched QA processes
are elevated. In-box WinRT display/clicks did work in that session. This is not
evidence that Windows App SDK supports elevated apps (Microsoft says it does not),
and is not a standard-user-token test. No UAC/security/notification setting was
changed. `ToastNotifier.Setting()` returns 0x80070490 for the tested identities:
reported as **unknown**, not enabled. Other query errors fail dispatch. Explicit
Windows disabled statuses prevent Show; OS notification/Focus policies win.

## Architecture and persistence

- `native/windows_notifications.cpp`: one MTA COM context, callbacks, bounded live
  subscriptions, mutex callback shutdown gate; historical clicks use COM factory.
- `native_notifications.py`: explicitly typed ctypes ABI, thread-affine transport,
  escaped text/XML; callback copies data to a Queue, never invokes Tk.
- `notification_backend.py`: opt-in registration and one blocking command worker.
  Source/frozen executable + data-path hashes isolate AUMID/CLSID. Registration
  is per-user and owner-checked; cleanup only removes this service's registrations.
- `reminder_engine.py`: pure calendar rules + cached heap + durable dispatch state.
- `reminder_ui.py`: queue consumer, settings, and existing search reveal routing.
- Existing tray/UI command poll consumes native events; no second Tk poll loop.
  Existing reminder timer remains bounded (1–15 seconds), including while hidden.

No Event fields or calendar Schema changed. Existing `Store.settings` contains:

```
native_reminders: {version: 1, enabled: false, time: "09:00",
                  general_days: 0, ddl_days: 1, private: false,
                  events: {event_id: {mode: "off" | "custom", days: 0|1|3, time: "HH:MM"}}}
native_reminder_state: {version: 1, deliveries: {plan_hash: {status, at, attempts}},
                       snoozes: {event_id: {base, key, at}}, burst: {at, count}}
```

Missing settings default **off**, with no notification registration or sends.
Unknown reminder format versions stop reminding, not force-reset data. Atomic
Store saves, future-schema refusal and backup protection apply unchanged. Backups
already include the settings. The native ledger is separate from legacy
`Store.notified` (600-entry cap). Exactly matching old notified timestamps and
valid legacy future Snooze metadata are imported only after opt-in; Event fields
are not rewritten. Configuration and dispatch bookkeeping do not create undo
commands; existing reversible event commands remain the sole event CRUD path.

## Date, dispatch and failure semantics

- General/urgent: existing effective first date. Native DDL/end-as-DDL: existing
  effective final date. Workdays/holidays/leave use Store's existing calculation.
- One plan per event, not one per occurrence day. Date-only events use configured
  local clock and selected lead (0/1/3 days). This clock is **not** a deadline.
- Explicitly timed events preserve the editor's minute advance; None remains
  opted out. A per-event custom date rule in settings overrides this if desired.
- Per-event overrides are managed in More → 提醒设置, not additional Event fields.
- Persist a dispatch reservation before Show. Failed saves prevent dispatch and
  roll back the in-memory settings. Show acceptance is acknowledged separately.
- Persistent event-ID/schedule fingerprints prevent repeats across UI rebuilds,
  restart, sleep, backward clocks and view changes. Title/notes/color don't form
  the identity. Relevant changes invalidate plans; bulk/delete/completion remove
  pending plans and active notifications through the same refresh hooks.
- Local wall times follow Windows timezone changes. A repeated DST wall minute
  uses the same dedup key; a skipped minute is eligible for bounded catchup on the
  next observed clock. No OS clock/timezone setting is changed by tests.
- Catchup window: 24 hours. At most 3 dispatch attempts per 5 minutes across
  restart; no wake-up torrent. Older unissued plans are ignored. Each plan has at
  most 3 attempts, with 2-minute retry delay. Failures are not marked success.
- Crash between OS acceptance and durable acknowledgement can cause a bounded
  retry. Stable Windows tag/group replaces its history item; this is not a promise
  of mathematically exactly-once visible banners across that unavoidable gap.
- No full event scan/UI rebuild per periodic tick; heap rebuilds only when dirty.
  The delivery ledger retains stable successful identities instead of silently
  pruning them and causing re-sends. Very long-lived large ledgers may eventually
  require explicit history maintenance (not included in this phase).

## Snooze, clicks, privacy and lifecycle

Native buttons: 10/30/60 minutes. Snooze persists separate from Event date/DDL/done;
replaces any prior pending Snooze. Completion/deletion/date-rule changes and
global off cancel pending Snooze. Settings offers explicit cancel. Per-event
preferences survive delete/undo of the same ID; they never create a new event.

Notification arguments contain stable ID/plan/action only. Input shape, current
plan and event existence are checked. Callback dedup and a bounded pending queue
defer actions while modal/update/native data operations own the app. Body clicks
restore the tray window then use `_activate_search_result`: date selection,
filter reveal, highlight/scroll and original editor. Deleted/stale IDs get safe
feedback. Exit disables callbacks before releasing the MTA/registrations.

Private mode replaces the title with generic text. Notes, backups and data paths
are never used as notification text. Image paths are local app assets only.

No exit-time scheduling or supported cold-start activation: own notifications are
cleared on shutdown, COM registration removed. A late activation flag returns
before CalendarApp/Store construction; it cannot launch a second calendar.
Habit 2.0 reminder settings/scheduling stay on the existing path, not a new native
habit reminder engine. No autostart/installer/License changes.

## Verification and limitations

- Final automated suite: 704/704 passed (626 baseline + 78 new tests). Python
  compilation, diff/whitespace/conflict-marker checks passed. No app.py theme
  colors were introduced. Native bridge compiles with the installed toolchain.

- Source and standalone PyInstaller notification gate: real user-confirmed name,
  icon, click and hidden-tray restore; 192 DPI. No real calendar data accessed.
- Real source CalendarApp with isolated synthetic Store: native 10-minute button,
  persisted Snooze, test-clock advancement, second notification, real editor
  reveal, grab release, unchanged event date; user confirmed.
- Packaged CalendarApp QA: native notification/body-click/editor user confirmed;
  user clicked body rather than Snooze. **The EXE native Snooze button was not
  separately manually verified.** Initial source harness closed its editor upon
  success; the user accepted that cleanup behavior, not an app regression.
- Four themes × Compact/Global × pinned on/off (16 real Tk cases), physical 192
  DPI: settings scroll, validation, modal ownership/release, Ctrl+K, fake queued
  hidden reveal/Snooze pass without notification sends. 96–192 scaling logic uses
  the existing automated DPI tests, not a claim of five physical monitor tests.
- Existing Shell/Tk tray regression passed all four themes, modal/geometry/pinned
  checks, 20 hide/restore cycles, taskbar minimize and loss-recovery protocol.
  A normal `app.py`-entry QA product EXE also passed 10 startup/exits, 20 cycles,
  icon removal, no hidden data writes, handles 374→374, idle CPU 0.0312s/3s,
  physical 192 DPI. The TaskbarCreated test simulates the Shell protocol; Explorer
  was not forcibly restarted. No formal Release package was overwritten.
- Pure tests cover calculations, leap/cross-month/year/skip dates, opt-out,
  persistence/backup, retries/crash boundary, privacy, throttling/1000 events,
  old metadata, cached scheduling, callback thread contracts, lifecycle routing.
- Standard-user-token execution, physically changing timezone/DST/sleep, OS
  permission toggles and Focus modes were not manually changed/tested on this
  machine. Mock tests cover disabled/error statuses. These are explicit manual
  regression items, not claimed passes. Actual notification behavior is subject
  to Windows build/user policy.

## Developer checks

Build bridge (existing toolchain only):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build_native_notifications.ps1
```

`build_release.ps1` builds/bundles this DLL; it installs nothing. End users need no
compiler. Standalone QA harnesses and generated reports/executables live under
ignored build/, not Release assets. No debug identity is embedded into the product.

Tools: `check_native_notifications.py` (synthetic transport gate),
`check_native_reminder_app.py` (opt-in real CalendarApp synthetic notification
integration), `check_reminder_ui.py` (no real sends, isolated real-Tk settings).
Never run a developer build against real data to test reminders.

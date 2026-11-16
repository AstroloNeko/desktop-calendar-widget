# B3c-C1 — Desktop storage startup

## Scope

C1 wires the existing desktop entry point to storage authority selection. It does
not enable migration of the normal user profile, change JSON Schema 9, change
SQLite Schema 1, update backups/updaters, build an EXE, or enter C2.

`app.main(arguments)` → `StartupOptions` → `open_desktop_storage` → lifetime
`DirectoryLease` → B3b `_select` → Store/Repository → `CalendarApp` → existing
history/Application Services → lazy shared `CalendarRuntime`/TkDispatcher/HTTP.
ReminderEngine consumes that same Store. No Event/Habit/Category algorithms are
duplicated. Creating a Chat window never constructs a second calendar Store.

## Public startup interfaces

- `StartupOptions(isolated_directory=None, offer_migration=False)`.
- `open_desktop_storage(options, confirm=..., notify=...)` returns a
  `DesktopStorageSession` owning the selected Store, authority state and lease.
- `create_desktop_app(options, app_class, instance=None, ...)` uses that Store
  and cleans up a partial UI construction on failure.
- `stop_desktop_storage(app)` stops admission/notifications/tray before closing
  the repository and releasing the lease. Normal close and mainloop exit both
  use this idempotent boundary.

Explicit isolated launch:

```powershell
py -3 app.py --isolated-data-dir C:\path\to\synthetic-profile --offer-storage-migration
py -3 app.py --isolated-data-dir C:\path\to\synthetic-profile
```

The isolation option rejects the configured normal DATA_DIR and its descendants.
No APPDATA override is required. Normal launch retains the existing update guard
and single-instance mutex. Isolated launches use a directory-scoped lease instead
of competing with the user's real instance. The isolated update-install route is
disabled. All tests use synthetic temporary profiles.

## Authority states

| State | Desktop action |
| --- | --- |
| NOT_STARTED | JSON, irrespective of stray SQLite files |
| PREPARING / RECOVERY_REQUIRED | Refuse business startup; retain evidence |
| STAGED / ACTIVATING | Resume B3b source/protection/candidate validation and activation |
| ACTIVE | Validate database identity/schema/integrity/revision; SQLite only |
| Missing state with witness / corrupt state | Refuse, never create an empty replacement |
| ACTIVE restore operation | C1 refuses startup pending C2 reminder/epoch review |

`existing_only=True` allows production authority lookup but rejects new migration,
restore and preparation-abandon operations. ACTIVE missing/corrupt databases are
not reopened with an initializing fallback, and old JSON rewrites are ignored.

## Migration consent

The short-lived owned native dialog explains JSON detection, original protection,
exclusive access and SQLite authority; users may confirm or keep JSON. A progress
window appears during validation and an owned result message reports activation.
No calendar UI, Chat, reminder or tray service exists while awaiting consent.

The source hash is bound before consent and rechecked inside `migrate`'s lease.
The startup lease is released for B3b's independently leased migration call, then
reacquired before selecting/opening the Store. A competing startup in either gap
causes a safe failure; it does not bypass exclusivity. No partial UI is started.

## Runtime and shutdown

The same EventCommandHistory remains shared by Event and Habit operations. A2/A3
services, AI Gateway and TkDispatcher retain their existing contracts. Chat uses
the existing `ai-assistant` directory: initial migration does not substitute an
empty epoch folder or lose configuration/history. SQLite's parent remains the
profile root. Settings, notification state and business writes use the selected
repository; startup skips JSON automatic backup for SQLite.

Close first marks the application exiting/stopped. Late TkDispatcher operations
are cancelled, HTTP/model admission stops, native notifications/tray stop, then
the repository closes and lease releases. This does not synchronously wait for a
network LLM request on the Tk thread (which could deadlock a queued dispatch).
An already-running model request can finish without permission to write business
data. If resource shutdown raises, its application/session is retained instead
of intentionally releasing authority beneath it.

Old v0.10 executables do not recognize this lease. Full old-program/update
coordination remains C2; C1 makes no claim that old writers are prevented.

## Backup / restore boundaries

SQLite does not enter the JSON daily-backup or export/restore UI path. The UI
explains that C2 integration is pending, while retaining existing modal guards.
SQLite/isolated sessions cannot install an update. Normal JSON behavior remains.
B3b offline backup/restore APIs remain available for isolated tests; a restored
desktop is deliberately blocked until C2 implements reminder replay review and
pending-operation epoch handling. No ChatJournal schema changes are made.

## Verification

28 new automated C1 cases cover Schema 8 JSON, consent/cancel, source mutation,
activation/reopen, missing/corrupt authority, STAGED/ACTIVATING recovery, lease
contention/cleanup, original preservation, shared mutations/history, settings and
reminders, initial chat-directory preservation, restored-runtime refusal and old
backup UI refusal. Existing B3b subprocess crash tests remain in the full suite.

Real Tk tools added/extended:

- `tools/check_desktop_storage.py`: actual production factory with v0.10.2
  fixture, eight theme/view cases, Event editor/completion, Habit check-in,
  Category, date-state, Undo/Redo; restart through `app.main`, persistent identity
  and revision, original JSON unchanged.
- `tools/check_interactions.py --sqlite`: Flow/Timeline move/resize/cancel,
  search/editor/modal and shared undo; no argument retains JSON regression.
- `tools/check_calendar_chat.py --sqlite`: four themes × two views, Mock LLM,
  Event/Habit writes, same runtime, real authenticated loopback HTTP.
- `tools/check_system_tray.py --sqlite`: real Shell integration with native
  callback messages, using repository revision instead of WAL main-file bytes
  to detect unexpected writes. No real Explorer restart or physical click claim.

GUI testing uses Tk-generated gestures and widget callbacks in real Windows
windows at actual 192 DPI. Consent is programmatically answered in automated
checks, not claimed as manual acceptance. Other physical display DPIs, release
EXE installation, and production data migration are outside this C1 run.

Recorded results: full suite **1246/1246** (1218 baseline + 28 C1); C1
**28/28**; Python compile and `git diff --check` passed. The `app.main` first-
migration wizard passed owner/progress/desktop checks with automated consent. SQLite business GUI and
`app.main` restart passed; SQLite and JSON interaction suites passed all four
themes at 192 DPI. SQLite Chat passed eight theme/view cases and real loopback
HTTP. SQLite Shell checks passed four themes, pinned on/off, modal guards,
20 hide/show cycles, taskbar restore, TaskbarCreated/silent-loss recovery, and
10 service start/stops. Deliberately malformed ZIP fixtures emit duplicate-name
warnings; HTTP negative tests emit existing response-cleanup ResourceWarnings.
No real user data is used as a fixture, migrated, restored or reset.

## C2 prerequisites still open

1. SQLite backup/export/restore UI and automatic backup policy.
2. Restore replay/epoch review, stale pending-operation treatment in desktop.
3. Old EXE coordination and updater installation boundary.
4. Offline disaster recovery and production rollout policy.
5. Formal packaged upgrade acceptance before enabling real-profile migration.

# B3c-C2 — Backup, recovery and updater safeguards

Base: C1 `1a7d997`, branch `feature/sqlite-production-integration`.
No production migration switch, version bump, EXE build, release or real user
data migration. This stage intentionally uses scoped tests, not the full suite.

## Desktop backup

The existing file picker and confirmation UI remain. JSON uses Format 1 as
before. SQLite uses `desktop_backup.export_sqlite` → B3b
`create_sqlite_backup` → Repository `backup_to` / official SQLite Backup API.
It does not copy a live WAL main file. A validated container is staged beside
the destination and published exclusively (or atomically replaces an explicitly
approved existing backup). Source/metadata paths are not backup destinations.

Format 2 covers events, routines and histories, categories, date states,
preferences, notified/delivery/snooze state. Identity, schema, size and SHA256 are
validated. ChatJournal, external AI configuration, HTTP credentials and DPAPI
files are excluded and the success message says so. B3b credential-key rejection
is retained; arbitrary user notes are not a universal secret-content scanner.

SQLite startup now makes a daily Format 2 backup in `backups`, never a stale
JSON backup. Existing same-day containers are checked. Failure becomes the
existing backup warning, not a success claim. Automatic retention cleanup is
not added; capacity/retention policy needs C3 review.

## Normal restore

SQLite UI recognizes Format 1 Schema 8/9 and Format 2. It shows backup time,
overwritten-change risk, reminder replay risk and loss of old pending/Undo
context. Confirmation binds the backup SHA256; lifecycle rechecks it under its
lease and after candidate construction. The existing UI then performs normal
shutdown, preserving the instance mutex during the restart request.

`app.main` loops: stop old Runtime/Tk owner/repository, acquire offline lifecycle
lease, protect current DB with Backup API, validate a new candidate, activate
via the existing STAGED → ACTIVATING → ACTIVE protocol, create a fresh desktop.
There is no live-file overwrite. A competing writer in the lease transition
gap causes refusal. Failure before staging retains authority; interrupted staged
restores resume using B3b checks. An error stops this launch and displays a
diagnostic rather than reopening expired JSON or repeating a restore blindly.

`candidate_from_backup` is shared by normal and disaster restores, using the
existing strict importer for legacy Format 1, not a second migration engine.

## Offline disaster recovery

Explicit launch (no Store/Runtime required):

```powershell
py -3 app.py --isolated-data-dir C:\path\to\synthetic-profile --recover-storage
```

Without `--isolated-data-dir`, the same explicit recovery UI addresses the normal
profile; it is never automatically executed and was not used on real data here.
Startup errors mention this entry point. The UI selects a backup and requires
replacement/reminder-replay consent before calling `disaster_restore`.

Under the directory lease it archives all top-level SQLite database candidates,
associated `-wal`/`-shm` files, and raw authority metadata into a unique
`disaster-original-<epoch>` folder. Each copy is exclusively created/fsynced and
hash-compared to the original. Inventory records the hashes. It does not open
the damaged database. Original files stay in place, unchanged; archive failures
cannot overwrite them. Symlink/confined-path checks reuse B3b.

Only after archival does it construct/inspect the independent backup candidate.
The new `disaster_restore` operation uses the same activation state machine;
its source verifier checks archived/original database bytes rather than trying
to read damaged SQLite identity. Metadata originals remain archived. No valid
backup means no activation and no fallback. Missing both state and management
witness is refused as ambiguous, requiring diagnosis.

Crash boundaries:

- ARCHIVED: old authority remains (possibly unreadable), archives retained; retry
  explicit restore, never expose an empty calendar.
- STAGED / ACTIVATING / PLACED: validate immutable source/archive hashes and
  candidate identity, then resume normal publication.
- ACTIVE: new named database is sole authority; original damage remains evidence.

Tests actually terminate child processes at these points with a damaged source.
Power-loss guarantees beyond B3b's file fsync/replace boundary are not claimed.

## Runtime, pending operations and reminders

Each restore persists a new epoch and explicit `reminder_replay_reviewed` flag.
Legacy restores lacking this review still refuse desktop startup. The user
explicitly accepts that an older ledger may resend notifications; no delivery
history is invented or merged and no reminder algorithm is changed.

The desktop is reconstructed, giving a fresh Store, history, reminder engine and
TkDispatcher owner. Old owner is stopped, repository closed, late UI writes
cancelled. Chat keeps the existing external encrypted configuration/token but
uses `ai-assistant/epoch-<epoch>/sessions.sqlite3`. Old journal/history stays on
disk and is not current fact; previous session/Pending IDs are absent from the
new journal. This persists across restart without changing ChatJournal Schema.
Old network model work may finish against its stopped owner/old journal; it is
not replayed into the restored calendar. No AI feature tests were needed.

## Updater and Repair

Before staging a payload updater, the shared `update_payload` path (also used
by Repair) checks managed state. ACTIVE requires an exact compatible
`storage-capabilities.json` and matching `app-version.txt` inside the verified
archive; unfinished/uncertain states refuse. Missing metadata in old releases
therefore blocks their updater before it is launched.

Build script now emits capability metadata (SQL schema/lifecycle/epoch support)
without changing the version or performing a build. This is a contract inside
the already checksum-verified official payload, not a signature or proof against
a malicious distributor fabricating capability declarations. C3 must verify the
packaged EXE actually implements the declaration.

Updater still waits for the parent and checks the known legacy instance mutex.
It then reserves installation, acquires the profile lease, selects authority and
rechecks payload capability. JSON gets its original backup; SQLite gets a real
Format 2 before-update backup. Copy occurs while leased. Overlapping install/data
directories are refused. Any gate/backup/copy failure prevents restart; there is
no fallback launch or authority rewrite. Complete installer-copy atomicity is
not newly claimed: a partial copy may require repair, but does not switch data.
Isolated desktops still cannot install over the real program.

Old v0.10 binaries ignore the new lease and can still be launched manually and
edit their JSON. They cannot be universally prevented by this change. Managed
SQLite never adopts those writes. Production migration stays disabled; C3 owns
real EXE/old-process coordination, package compatibility and rollout acceptance.

## Validation scope

New C2 suite covers Backup API, failed export, daily SQLite authority, offline
consent/lock, settings/ledger preservation, closed old repository, epoch journal
isolation, Format 1 Schema 8/9 and Format 2, archive failure, corruption/missing DB,
malformed state, no valid backup, real child-process interruption, old JSON
changes, and updater pre-stage/copy/restart gates.

Selected regression includes Repository Contract, SQLite Repository, Schema 8/9
migration, storage lifecycle, C1 startup, JSON/SQLite backup UI, updater and
Repair. Existing intentionally duplicated ZIP fixtures produce warnings.

`tools/check_backup_recovery.py` uses real Modern Tk at actual 192 DPI and the
production `app.main` restart loop. It exercises UI export, normal restore,
fresh Store/history/reminder ownership, and offline damaged-database recovery.
The minimal lifecycle extension checks the existing HTTP listener stops/rebinds,
uses the new Store/journal epoch and retains its external credential; no model
request or conversational feature is exercised.
File pickers/confirmation are automatically answered; this is not manual GUI
acceptance. All input is synthetic and all writes are in temporary directories.

Not run: full 1200+ suite, four-theme matrix, Flow/Timeline/drag regression,
Chat GUI, Mock/real LLM, unrelated HTTP features, formal EXE upgrade. These remain
outside C2; complete RC acceptance belongs to C3.

## Recorded results

- Scoped regression: **294/294 passed**, comprising 12 affected test modules.
- After the final corrupt-epoch guard: **20/20 C2 tests passed** (19 of these
  were already in the scoped run; one additional epoch case). No claim of a
  new full-suite run is made.
- Modern/192 DPI backup/restore/offline GUI smoke passed, including the minimal
  HTTP stop/rebind and epoch checks described above.
- Python compile and `git diff --check` passed. Build-script syntax checked only;
  no executable or formal package was built.

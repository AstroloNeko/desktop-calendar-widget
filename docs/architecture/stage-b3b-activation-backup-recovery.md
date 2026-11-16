# Stage B3b — isolated activation, backup and recovery

Base: `3d4792f`, 1191 tests. No production startup/backend, updater, backup UI,
ChatJournal schema, version or user data changes. New APIs require explicit
`StorageLifecycle(directory, isolated=True)` and reject the formal DATA_DIR and
its descendants. This opt-in is an API guard, not an OS security sandbox.

## State / authority

```
NOT_STARTED → PREPARING → STAGED → ACTIVATING → ACTIVE
                   ↘ explicit abandon → NOT_STARTED
uncertain/corrupt state → diagnostic refusal (no writer)
```

| State | Authority / action |
| --- | --- |
| NOT_STARTED | JSON; stray SQLite files do not select a backend |
| PREPARING | JSON is still authoritative, but managed startup refuses writes until explicit review/abandon |
| STAGED | Original authority retained; validated transition can resume under lease |
| ACTIVATING | Neither backend is offered for writes until final placement/identity is resolved |
| ACTIVE | Exactly the named SQLite database; never re-import/fallback to JSON |
| RECOVERY_REQUIRED / malformed/missing managed state | Refuse startup; preserve files |

`storage-state.json` format1 contains operation, phase, persistent epoch, source
identity/hash/schema, original protection name, candidate/final names and hash,
calendar ID/revision/schema, and report hash. Restore records previous database,
its identity, pre-restore protection/hash and previous epoch. `.storage-managed`
is a separate witness: losing state after management begins is not equivalent to
NOT_STARTED. Missing both state and witness is outside this local corruption
detector; B3c must protect activation metadata as part of deployment/backup policy.

Named paths are basename-only, confined to the resolved directory; symlink paths
are rejected. State writes use unique same-directory temporary files, fsync and
os.replace. Windows directory-entry persistence across power loss is not claimed.
Individual file replace and SQLite COMMIT are not one cross-file ACID transaction.

## Lease and runtime

`.calendar-lease` uses nonblocking msvcrt byte locking on Windows (flock on POSIX).
Handles are held throughout migration/restore/backup or the entire managed
Headless Runtime lifetime. A second cooperating process fails with directory_busy.
Process termination releases the OS lock. There is no PID-only stale-lock guess.

`runtime()` calls existing HeadlessRuntime and A2/A3 services; JSON defaults in
app.py/Store are unchanged. Restore cannot acquire a lease while that Runtime,
its model request or managed HTTP service is alive. Shutdown failure retains the
lease and Runtime in quarantine; retry_shutdown explicitly retries the existing
stop mechanism. No filesystem swap is allowed through that lease while stopping.

These guarantees apply only to this explicit lifecycle API. Raw Store,
HeadlessRuntime without this wrapper, old EXEs and external SQLite clients do not
honor the lock. B3c must route production composition through it and coordinate
old processes. This implementation does not claim otherwise.

## Migration and recovery boundaries

1. Lock, strictly validate explicit calendar_data.json (Schema8 or 9).
2. Persist PREPARING, exclusively create original-<epoch>.json with fsync;
   verify bytes via SHA256 plus current source inode/device identity.
3. B3a dry-run in a unique stage directory. No source JSON rewriting.
4. B2 Backup API makes a standalone candidate, avoiding WAL-only contents.
5. Persist STAGED with report hash, database hash, identity/revision.
6. Revalidate source/protection/report, candidate hash/schema/business fields,
   quick_check/FKs, migration receipt and its committed revision.
7. Persist ACTIVATING; publish a same-volume hard link at a unique final name.
   os.link refuses overwrite. Validate final, persist ACTIVE, unlink candidate.

The original JSON and separate original protection remain; neither is a live
JSON mirror. No retention cleanup is implemented. The stage report remains a
B3a report with switch_allowed=false; only the lifecycle ACTIVE state chooses
authority, not that report or file existence.

Crash at STAGED/ACTIVATING: rerun checks and complete publication. Crash after
placement but before ACTIVE: exact hash/identity must match, then record ACTIVE.
Crash after ACTIVE: normal SQLite startup permits revision growth, verifies
identity and integrity, and ignores later writes to historical JSON. Candidate
link residue after an ACTIVE crash is not selected as authority.

PREPARING interruption never guesses success. `abandon_preparation()` is explicit,
checks original source hash and absence of the final file, archives prior state,
then enables a fresh attempt. It cannot abandon STAGED/ACTIVATING/ACTIVE. Other
uncertain states require diagnostics, not silent rollback. Source/candidate/report
changes reject resumption. No automatic retry into a stale candidate.

ACTIVE corruption/missing database fails before the Repository can initialize
an empty file. JSON is never selected. Damaged active database/metadata repair is
deliberately not automatic; preserve the originals and require diagnostic recovery
before invoking the normal restore API. The normal restore path requires a
readable authoritative database so its protection is verifiable.

## Backup format 2 and old format 1

`calendar_sqlite_backup.py` is separate from the unchanged product format1 UI.
Format2 has exactly manifest.json + calendar.sqlite3. Manifest includes app
version, backend, UTC creation time, SQL schema/calendar ID/revision, member size
and SHA256, included and excluded scopes. Snapshot uses Connection.backup through
B2, not a live main-file copy. Validate snapshot before packaging.

Included: all core entities/histories, device/unclassified preferences and reminder
delivery bookkeeping in the calendar database. Excluded: external AI config,
ChatJournal, HTTP credentials and DPAPI files. Unexpected secret-bearing preference
keys block export (no silent field stripping). Arbitrary notes/unknown preference
text remain user data; keyword detection is not a general secret-content scanner.
Unknown database tables are refused, not unknowingly exported.

Allowlisted bounded ZIP reads; exact two distinct filenames, 64MiB data and 64KiB
manifest limits, archive size bound, no encryption/directories/symlinks/special
types. No extractall on supplied backups. Check hash/size/format/identity and
strict persisted business fields. Hashes detect corruption, not authenticate a
malicious author who rewrites both data and manifest; no signature is claimed.

Format1 is verified by existing read_backup, then its raw JSON goes through B3a.1
strict conversion. Test fixture backup is generated by the actual immutable
v0.10.2 source in TemporaryDirectory, not merely by current create_backup.

## Restore lifecycle and reminders

`restore(backup, authorize_replacement=True)` requires explicit replacement
authorization even for equal/newer revisions (revisions across calendars are not
comparable clocks). Require offline exclusive lease. Validate into new candidate,
Backup-API-protect the current DB, check protection identity, write STAGED, then
reuse ACTIVATING/ACTIVE recovery with a new file and epoch. No live file overwrite,
old WAL mixing or deletion of previous authority. Failed pre-STAGED preparation
leaves current ACTIVE intact; later failure blocks pending validated recovery.

Reminder settings and delivery/snooze records are restored as backup facts.
An older backup can forget deliveries made since it was taken; therefore a
notification backend on a restored runtime requires explicit
`acknowledge_reminder_replay=True`. Without review it is refused, not silently
replayed. No attempt to merge two ledgers or invent delivery history.

## ChatJournal / Undo boundary

Each epoch owns `runtime-<epoch>` AI configuration/journal directory. Restore
creates fresh service sessions, authentication configuration, Store and Undo
history. The wrapper rejects caller-supplied journal/config/repository overrides.
Old sessions/Pending cannot resolve in the new Journal, including after restart;
old IDs must be queried again. Old completed messages remain archived in the old
directory but are not automatically shown as current facts or replayed. AI must
be reconfigured in this isolated mode; desktop account migration is not added.
No ChatJournal schema change or cross-database exactly-once claim.

## Old executable risk / B3c gate

v0.10.0–.2 use APPDATA/DesktopCalendar/calendar_data.json, support up to Schema8,
and do not know this state/lease. They can still write a preserved Schema8 JSON.
Tests modify that path after ACTIVE and prove managed runtime stays on SQLite.
The updater currently checks its known DesktopCalendarWidgetV2 mutex; arbitrary
old copies/headless paths cannot be assumed covered. B3c must coordinate shutdown,
route new startup through authority checks, isolate paths or provide a verified
old-reader gate, and test actual packaged upgrades. Never claim a new lock alone
blocks old binaries. No updater changes in B3b.

## Evidence and remaining limits

Tests cover real child-process os._exit at PREPARING and four migration/four
restore checkpoints, restart recovery, cross-process lease contention, source/
protection/candidate/report tamper, missing/corrupt state and active database,
storage fault injection, old-code backup conversion, ZIP hostility, explicit
replacement consent, lifecycle journal isolation, services/Undo and loopback HTTP.
Existing B2 tests cover WAL Backup API/COMMIT rollback; B3a tests cover import
failure/integrity/semantic mismatch. All data is temporary and synthetic.

Final full regression: 1218/1218 passed (71.415s), 27 new tests; final B3b
specialty rerun also passed. 157 Python files compiled. Expected negative HTTP
and duplicate-ZIP fixture warnings were emitted. Initial sandbox hard-link
denial was rerun under normal Windows permissions, without changing the mechanism.
The first full run used two incorrect test expectations (short HTTP token and
missing Journal result expected as null); corrected to the existing contracts,
then all tests passed. No product authentication rules were weakened.

JSON GUI smoke: modern/aero/paper passed initially; Frutiger simulated Esc cancel
left a canvas grab and cleanup failed. Unchanged script, run separately, passed
all four themes at actual 192 DPI including Compact/Flow/Timeline, drag/resize,
Undo, search and modal. This intermittent input/focus failure is recorded, not
erased or claimed fixed. GUI does not load the new lifecycle modules; no window
code changes were made. SQLite Headless/Mock Chat/real loopback HTTP are exercised
by the new tests. No paid model, packaged EXE or human exhaustive GUI acceptance.

Performance sample (seconds, concurrent local tests; not an SLA), with 10 habits,
120 historic rules and 20 completions per dataset:

| Events | Migration | Backup | Restore | First Runtime open |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 1.165 | 0.142 | 0.242 | 0.189 |
| 1000 | 6.825 | 0.527 | 1.075 | 0.306 |
| 10000 | 59.769 | 2.668 | 6.327 | 1.543 |

10,000 source validation/import/integrity/comparison: 1364/1656/63/50900ms.
Source hashes unchanged. `tools/check_storage_lifecycle.py` reproduces the run.

Remaining release blockers: production startup and updater integration, old-reader
protection, UI for review/recovery and reminder replay consent, archival policy,
physical power-loss testing and signed/trusted backup policy if required. Damaged
authority repair requires a separate explicit workflow; it is not disguised as
successful fallback. No B3c, EXE build, production migration or default switch.

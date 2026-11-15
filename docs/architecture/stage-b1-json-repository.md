# Stage B1 — JSON Repository boundary

## Scope and authority

Based on A4 `e9a7e3239932eb19576c4a625b3d2fd50f995040` (1028 tests).
Store remains the **only authoritative owner** of events, routines, categories,
date states, preferences and notification state. No second mutable cache or file
is introduced. Schema 9, paths, field meanings, ordering and backup format remain
unchanged. B1 is not a SQLite implementation or a cross-process transaction system.

## Contract and adapter

`calendar_repository.CalendarRepository` exposes detached get/list reads for
Event/Routine/Category, date states, preferences and a calendar snapshot.
`apply_changes(changes, expected_revision=...)` accepts a prepared mapping of
affected collections (`events`, `routines`, `categories`, `date_states`, `notified`)
and returns `RepositoryCommit(changed, revision)`. It is an internal persistence
port, not a new user-facing CRUD API: A1 validators and Store business preparation
must run first. Unknown fields, invalid collection types, empty/duplicate IDs and
invalid date states are rejected before publication.

`JsonCalendarRepository` implements the contract. `read_document`/`write_current`
are explicit transitional Store integration ports. Store still normalizes legacy
documents and owns date expansion, DDL, color, Habit schedule/history and reminder
rules. The adapter owns JSON decoding compatibility gates, encoding, migration
checkpoint, temporary-file replace and commit failure handling.

Public reads recursively copy mutable records and preferences. Mutating them
cannot change authority. Prepared commits copy new/changed records; unchanged
canonical objects are reused only inside the explicit Legacy Bridge, avoiding a
deep copy of every record for a single-record mutation. Callers must not mutate
canonical records during preparation. Query snapshots carry no writer; the legacy
search Store copy has its repository capability removed.

## Entry points and remaining bridges

| Path | Current boundary |
|---|---|
| Desktop Event, bulk, drag/resize, AI Event | Existing A2 service/history → Store.persist_event_states → repository.apply_changes |
| Habit create/update/complete/delete, category, date ranges | A3 service/Store preparation → _commit_fields → repository.apply_changes |
| Event patch, category before-state | Service repository detached read |
| AI event/habit/category searches | Repository list snapshots; Store still supplies shared rule calculations |
| Headless snapshot | Runtime repository snapshot; existing external shape preserved |
| Event/Habit Undo/Redo | Existing single EventCommandHistory; same persistence port, no second stack |
| Backup/restore | Existing file format/checkpoints; preserve active repository identity after staging Store swap |

Remaining direct Store interfaces are deliberate, not fully migrated:

- Command capture and Store's prepared-list algorithms access canonical objects.
  Legacy upsert/delete APIs remain for compatibility/tests; main business UI uses
  A2/A3. Rendering and rule queries still read Store.
- `app.py` direct saves are geometry/view/theme/opacity/holiday preferences,
  initial text-setting migration and legacy reminder completion state; no new
  Event write path was added. `global_hotkey.py` saves device preferences.
- `reminder_engine.py` retains its delivery-ledger rollback path; these are local
  device states, not new domain event transactions.
- Backup synchronization and staging use Store.save/load; test fixtures can seed
  Store directly. All JSON writes now delegate to the adapter, but legacy saves
  do not acquire the full prepared-change rollback contract.
- ChatJournal and AI configuration storage remain independent and unchanged.

Future core storage includes Event, Routine histories, Category and date states.
Device geometry/theme/hotkeys and delivery bookkeeping should remain explicitly
distinguished; B1 does not split their existing JSON representation.

## Commit, failure and revision semantics

No-op commits save zero times. Multi-object prepared changes publish once and
invoke Store.save once (a compatibility hook delegating to write_current, not a
second write). Batch and shared Undo semantics remain intact; previews do not
write. UI effects still occur after successful business commit.

The pre-commit disk SHA-256 is compared on failure. If bytes are unchanged,
affected in-memory references are restored. If bytes changed or cannot be read,
`MutationUncertainError` blocks further writes through load_error: do not retry.
Event service now maps this to `execution_uncertain`, separately from validation,
conflict and storage errors. No command is appended on an uncertain commit.

`SnapshotToken(session, generation)` is process-local. Successful saves/reloads
and backup restores invalidate tokens; expected token mismatch rejects before
writing. The token is not stored in JSON, is not an object revision, and cannot
detect unsaved mutation through legacy references or concurrent external writers.
Existing command previews separately compare object snapshots and actual expanded
dates; holiday/leave changes invalidating the dates still reject stale previews.
There is no process lock, cross-process ACID or JSON/ChatJournal exactly-once claim.

## Runtime injection

Store accepts `repository_factory(store)` before loading. HeadlessRuntime forwards
the factory; desktop can supply a Store constructed with that factory. Services,
gateway and CalendarRuntime share that Store-owned adapter. CalendarRuntime
rejects a different repository instance, preventing dual authority. Headless
continues using one business executor; no transaction waits for model network I/O.

Remaining coupling: the factory receives legacy Store, models remain in
calendar_core, and backup staging still assumes JSON. A future backend must adapt
these bridge ports or narrow them further; B1 does not claim a drop-in SQLite
backup implementation. Domain service code does not branch on backend type.

## Verification

Added 16 repository contract tests and 12 JSON-specific tests (28 total). Factory
replacement allows future adapters to reuse behavior tests, although service/Undo
tests intentionally still depend on the shared Store bridge.

- Complete suite: **1056/1056** passed, including A1–A4, mixed Undo, batch, Chat,
  loopback HTTP, backup and compatibility tests.
- Final run: 31.327 seconds; 142 Python files compiled; git diff --check passed.
  Suite emitted HTTPError cleanup ResourceWarnings and the deliberately duplicated
  ZIP entry warning; neither failed a test. Initial sandbox hard-link permission
  failure was rerun successfully with normal Windows permissions.
- Failure injection: temp write, replace before/after, unreadable outcome, memory
  rollback, uncertain blocking, no extra history/save, detached reads and restore
  ownership covered.
- Schema 1–9 gate test uses **synthetic** documents, not nine archived authentic
  release fixtures. Existing representative legacy fixtures also run. Malformed
  legacy records retain tolerant loading; duplicate Event IDs are not silently
  merged, targeted repository access/commit rejects ambiguity. Strict import
  validation belongs to B3. Untimed `23:59 + has_time=False`, non-UUID IDs and
  unknown settings are retained; future schemas are refused without rewriting.
- Real Windows Tk smoke: four themes × Compact/Global, Habit/category/date ranges,
  Undo and Modal passed; four-theme Flow/Timeline move/resize/cancel, search and
  editor passed. Chat GUI 8 theme/view cases, Mock model, real loopback HTTP and
  modal/search/Undo passed with errors=[]; actual **192 DPI / 200%**.
- GUI uses programmatic Tk gestures and isolated temporary data. This is not a
  claim of manual exhaustive visual review, real model testing or packaged EXE
  validation. No production calendar data was modified.

Same interpreter (`C:\Python314\python.exe`, Python 3.14.7), seven-run medians,
1000 synthetic events; A4 extracted into a temporary directory from its commit:

| Measurement (ms) | A4 | B1 |
|---|---:|---:|
| Detached list of 1000 events | 6.040 | 6.267 |
| Save 1000 events | 22.461 | 22.729 |
| Shift 50 + Undo + Redo | 98.140 | 114.018 |

This small local benchmark is not an SLA. Batch cost increases about 16% (16ms
across three commits) from validation, changed-record copying and Event disk
digest protection; ordinary query/save cost is similar. No full-authority copy
was added to each ordinary write.

## B2 prerequisites and risks

Preserve all these snapshot, ordering, identity, atomic batch, history and failure
contracts. Replace session generation with real persistent revisions/constraints
only with an explicit migration plan. Account for restore ownership, legacy
prepared lists and settings/reminder bridges. Separate strict migration validation
from tolerant legacy loading. Do not treat the present file-digest safeguard as
multi-process concurrency control. B1 stops here; no B2 migration was performed.

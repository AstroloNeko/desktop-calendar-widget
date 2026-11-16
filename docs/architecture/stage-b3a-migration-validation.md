# Stage B3a — strict JSON → SQLite rehearsal

## Scope and interfaces

Based on B2 `539f152`; desktop startup still selects JSON Schema 9. This change
does not discover user files, activate SQLite, migrate backups or change updates.
All executed tests use synthetic temporary files, never real calendar data.

- `calendar_migration.validate_source(source) -> ValidationResult`: explicit
  read-only input; metadata, SHA-256, raw counts, issues and normalizations.
  `valid` authorizes rehearsal only. The validated document is private and is not
  included in the diagnostic report.
- `dry_run_migration(source, staging_directory, *, clock=...) -> dict`: reserves
  new report/candidate paths, imports, checks, reopens and compares. Default clock
  is the deterministic naive `2026-01-15T12:00`; callers can supply another naive
  clock (years 2–9998). No Tk dependency.
- `SqliteCalendarRepository.import_snapshot(changes, preferences, source_hash=...)`:
  internal validated-plan adapter, not a replacement for source validation.
  Requires an unused revision-0 database and a complete five-collection snapshot.
  Reuses B2 `_apply`, transaction and receipt machinery.

## Complete JSON9 → SQLite1 mapping

All IDs remain exact strings, including historical non-UUID IDs. Different IDs
with the same title/date remain separate. Boolean columns store 0/1 and read back
as bool; nullable values remain null. Source text is not converted to UTC.

| JSON path / fields | SQLite representation |
| --- | --- |
| `version = 9` | Independent `PRAGMA user_version = 1`, `schema_migrations`; exported compatibility document remains JSON9 |
| `events[].id, title, due, color, event_type` | Same-named `events` columns |
| `events[].reminder, notes, done, created_at, snooze_until` | Same-named `events` columns |
| `events[].has_time, duration_days, skip_non_working_days, end_as_ddl` | Same-named `events` columns |
| `events[].category_id, color_mode` | Same-named columns; deferred calendar/category FK |
| `routines[].id, title, kind, color, created_on, completed_on, enabled` | Same-named `routines` columns |
| `routines[].reminder_enabled, reminder_time` | Same-named `routines` columns |
| `routines[].frequency, interval, month_day, year_month, start_on, end_on, workdays_only` | Same-named `routines` columns |
| `routines[].weekdays[]` | `routine_weekdays.weekday`, routine ID and `order_key` |
| `routines[].habit_done[]` | `habit_completions.occurrence_date`, routine ID and `order_key` |
| `routines[].schedule_history[].until` | `habit_schedule_history.until_date`, exclusive boundary, original `order_key` |
| `routines[].schedule_history[].rule` | `rule_json`, all eight keys retained: `frequency, interval, weekdays, month_day, year_month, start_on, end_on, workdays_only` |
| `categories[].id, name, color, sort_order, created_at` | Same-named `categories` columns |
| Event / Routine / Category array positions | Per-table `order_key`; raw category order is retained on import independently of display sort |
| `date_states[day] = leave / holiday` | `date_states.day, state` |
| `date_states[day] = normal` | No override row; disclosed semantic normalization, system calendar untouched |
| `settings[key]` | `preferences.key, value_json, scope`; complete raw JSON value retained |
| `notified[]` | `reminder_delivery_state.plan_key`; every unique key, no 600-record pruning |

Additional SQLite metadata: B2 local calendar ID, `timezone=local-naive`, initial
import revision 1 (also for an empty calendar), entity revisions and an operation
receipt `migration:<source SHA-256>`. These are target metadata, not invented
source business fields. Unknown business/top-level fields block import rather
than disappear. Unknown settings are intentionally accepted and retained.

Preference scopes reuse B2: geometry/theme/hotkey/device keys → `device`;
reminder-named keys / notifications setting → `reminder`; others → `unclassified`.
This is storage classification, **not a sync policy**. Native reminder delivery,
snooze and burst state nested in settings stays intact in `value_json`, and is
included in full-field comparison. Legacy `notified` has its own record count.

## Strictness and compatibility policy

Parse raw UTF-8 (optional BOM), detect duplicate JSON keys, reject non-finite
numbers, invalid Unicode and excessive nesting. Input is bounded to 32 MiB.
Counts are taken from original arrays, not a tolerant Store projection.

Every current dataclass field is required. Validate field types (bool is not int),
integer bounds, nonempty unique IDs, valid dates/local-naive timestamps, colors,
event kinds, duration, category references, reminders and recurrence. Construction
through the existing model is a subsequent equality check: if it would silently
repair/drop/reorder a value, refuse migration. Missing fields do not acquire
guessed defaults or current timestamps. `23:59` plus `has_time=False` is preserved.

Habit rules use existing strict schedule validation. Completion dates must be
unique, valid and already in the order the model preserves. History retains each
rule and exclusive `until`; duplicate or unsorted boundaries are rejected, not
collapsed. Legacy `todo` remains supported. No completion/history is regenerated.

Disclosed, lossless semantic normalizations are limited to explicit `normal`
date overrides becoming absence and `notified` set ordering. A `workday` override
is not supported by current JSON9/SQLite1 and is rejected; statutory makeup work
days still come from the existing holiday provider.

Known compatibility limits: tolerant-loader-readable input is not necessarily a
lossless migration candidate. Examples rejected: omitted fields, old priority
aliases, unsorted completion history, duplicate history boundaries, inherited
raw fallback color differing from its category, timezone-aware timestamps,
unknown domain fields. These require a separately reviewed explicit conversion;
the tool does not silently normalize them. Schema 1–8 and future schemas are
rejected with `unsupported_schema`. Existing compatibility tests are retained;
new old-schema rejection fixtures are **synthetic, not authentic archived files**.
Old `.dcalbackup` import remains B3b work.

## Read-only source and candidate lifecycle

1. Read source bytes once, capture size/mtime and SHA-256.
2. Strictly validate before constructing any model or SQLite candidate.
3. Require explicit staging outside the formal data directory and source parent.
   Existing candidate or report files are never overwritten.
4. Reserve owned files; initialize a new Schema1 database.
5. Under one `BEGIN IMMEDIATE`, check empty database/revision, import all domain
   rows, preferences, delivery state and receipt; commit once. Schema creation is
   separate from the single atomic business-data import.
6. `quick_check`, `foreign_key_check`, close/reopen, full and semantic comparisons.
7. Rehash source before success and during finalization. A changed/unreadable
   source invalidates the candidate. No source writer is used.
8. Save JSON/text reports. Failures close connections and delete only this
   invocation's candidate, WAL and SHM; cleanup failure is an explicit issue.

No user-directory lock is claimed. Source hashing detects observed changes, not
changes after the last read; B3b must lock and revalidate at activation. Report
write failures also revoke candidate approval. If reports cannot be written, the
returned structured result is authoritative and the error is surfaced. There is
no resumable import or automatic retry into existing targets.

## Equality and semantic checks

Compare every normalized raw field against reopened SQL export, then both model
projections against the validated raw document. This independently catches a
tolerant loader dropping the same record on both sides. Arrays are order-sensitive;
JSON object key order is not semantic. Compare all settings, unknown keys, device
state and all notification keys, not just counts.

Use the same fixed clock and existing business functions on both sides:

- Every Event occurrence date, actual deadline endpoint and effective color.
- Date queries on first/last 16 occurrence days, all manual overrides, clock ±7
  days and year boundaries; stable Event order compared.
- Every habit's rules/history/completions checked structurally; execution probes
  for 732 anchor days, query days, all completion/history/end boundaries ±1 day.
- Habit progress over clock ±31 days, DDL groups and reminder plan projections.

Semantic probing is finite, not a claim of exhaustive recurrence over infinite
future years. Complete persisted fields are always compared; no new recurrence,
holiday or DDL algorithm is introduced.

## Reports and failures

`MigrationIssue` contains severity, stable code, JSON source path, object kind/ID
when known, message and blocking flag. Titles, notes, settings values and raw
exception messages are omitted. Paths/IDs/hashes are diagnostic metadata and
reports should still be treated as local data.

Example abridged synthetic successful report:

```json
{
  "status": "validated_candidate",
  "candidate_ready": true,
  "switch_allowed": false,
  "source_unchanged": true,
  "integrity": {"quick_check": "ok", "foreign_key_violations": 0, "sqlite_schema": 1},
  "comparison": {"equal": true, "phase": "semantics", "path": null},
  "issues": []
}
```

Full reports additionally carry source metadata/hash, original/imported counts,
normalizations, preference scope counts, timings, candidate path, revision/receipt
and semantic fingerprint. `switch_allowed` is always false, even on success.
Human report includes status, source hash, counts and localized issue paths.

## Validation results

- B2 baseline: 1110/1110 passed.
- New migration tests: 51; complete suite: 1161/1161 passed (34.641 seconds).
- Covers normal/empty/large/same-title data, all recurrence types, retained raw
  category order, >600 notification keys, defaults unchanged, privacy, malformed
  fields, references, schemas, duplicates, source changes and existing targets.
- Fault injection covers mid-write/commit rollback (zero partial rows), integrity
  failure, corrupted target, reopen failure, dropped records, unwritable staging
  and report failures. All use isolated data.
- Repository contracts and A1–A4/B1/B2 regression are included in the full suite.
- Final rerun: 1161/1161 passed (55.895 seconds); 150 Python files compiled;
  `git diff --check` and conflict-marker scan passed. Negative HTTP/ZIP tests
  emitted resource/duplicate-entry warnings, with no failures.
- Existing `tools/check_interactions.py` passed with isolated temporary JSON
  data on real Windows Tk at 192 DPI: modern/aero/paper/frutiger,
  Compact/Flow/Timeline, move/resize/cancel, undo, search and editor modal checks.
  This is scripted GUI coverage, not a new manual visual acceptance or packaged
  EXE migration test; B3a does not build or release an EXE.

Performance: Windows, Python 3.14.7 x64; `tools/check_calendar_migration.py`.
Each dataset includes 10 habits, 120 history rules, 20 completions, 1 category,
1 override and skip-day/end-DDL events. Timings are observational, not thresholds.

| Events | Validate ms | Import ms | Integrity ms | Compare ms | SQLite bytes |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 19.908 | 27.093 | 3.063 | 355.907 | 200704 |
| 1000 | 62.806 | 71.375 | 4.817 | 2161.229 | 462848 |
| 10000 | 470.828 | 556.254 | 19.564 | 20981.010 | 3162112 |

10,000-row comparison checked 50,000 actual occurrences and 7,330 habit probe
days. A separate 1,000-row tracemalloc run measured 33,688,400 bytes peak Python
allocation; it is not process RSS or SQLite native memory. Instrumentation makes
that run slower, so its timings are not mixed into the table.

## B3b prerequisites (not implemented here)

- Formal user-directory lock, process/update coordination and old-EXE protection.
- Revalidate source hash, candidate identity/integrity/schema and complete report
  while holding activation coordination; never activate an arbitrary stale file.
- Recoverable atomic backend selection, durable activation state and startup
  recovery, preserving original JSON and immutable pre-migration backup.
- Explicit conversion for unsupported legacy/noncanonical inputs with reports.
- Backup format/version strategy, SQLite WAL-safe backup/restore and rollback.
- Crash/disk-full/permission tests at activation boundaries and real packaged
  upgrade tests using isolated copies, without long-term JSON/SQLite dual writes.

No default switch, release/version changes, real-data migration or B3b activation
is authorized by this implementation.

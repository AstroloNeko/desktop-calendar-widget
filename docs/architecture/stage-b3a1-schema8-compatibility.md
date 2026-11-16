# B3a.1 — formal v0.10 Schema 8 compatibility

## Evidence and scope

Reference tag: `v0.10.2`, commit `cb0c62b204c553f61cb31b83955e1191f675c2e2`.
`v0.10.0..v0.10.2` has no diff in `calendar_core.py`, `habit_schedule.py`,
`data_compat.py`, `holiday_data.py` or `data_backup.py`. These releases save JSON8.
Commit `f7e622c` adds JSON9 and `RoutineItem.schedule_history`. This implementation
does not change these tags, default JSON storage, backups or user files.

## Complete structural difference

All Event fields (id, title, due, color, event_type, reminder, notes, done,
created_at, snooze_until, has_time, duration_days, skip_non_working_days,
end_as_ddl, category_id, color_mode), all Category fields (id, name, color,
sort_order, created_at), root collections, settings, notified and date_states
have unchanged persisted names/types/semantics.

All Routine fields except schedule_history already existed: id, title, kind,
color, created_on, completed_on, habit_done, enabled, reminder_enabled,
reminder_time, frequency, interval, weekdays, month_day, year_month, start_on,
end_on, workdays_only. No existing canonical field is renamed. Event aliases
`type`, `priority`, `is_ddl` are accepted by the old reader, not emitted by its
serializer. Known old priority labels map through the audited v0.10 map;
unknown values and conflicting aliases are rejected instead of silently lost.

The complete canonical JSON9 → SQL1 mapping remains in
`stage-b3a-migration-validation.md`. This document supersedes that document's
Schema8 rejection policy only; Schema9 validation remains strict.

## Explicit defaults and their source

Each applied rule is recorded with source path, target path, rule identifier,
lossless-semantic flag and source code evidence. No field values/credentials are
included. `calendar_legacy_migration.py` contains the audited default tables.

| Omitted field | Value / rule | v0.10 evidence |
| --- | --- | --- |
| Root events/routines/categories/notified | Empty list | Store.load `.get` defaults |
| Root settings/date_states | Empty object; effective settings still use runtime defaults | Store.load |
| Event color/event_type/reminder | `#6687F2` / general / 60 | Event dataclass and from_dict |
| Event notes/done/snooze_until | empty string / false / null | Event dataclass |
| Event has_time/duration_days | true / 1 | Event dataclass/from_dict |
| Event skip_non_working_days/end_as_ddl | false / false | Event.from_dict |
| Event category_id/color_mode | null / override | Event.from_dict |
| Untimed Event omitted reminder | null, not a 23:59 alarm | Event.__post_init__ |
| Inherited Event omitted color | Referenced category color | Store.load category inheritance |
| Category color/sort_order | `#6687F2` / 0 | EventCategory dataclass |
| Routine kind/color | habit / `#52B788` | RoutineItem dataclass |
| Routine completed_on/habit_done/enabled | null / empty list / true | RoutineItem dataclass |
| Routine reminder_enabled/reminder_time | false / null | RoutineItem dataclass |
| Routine frequency/interval/end_on | daily / 1 / null | RoutineItem dataclass |
| Routine workdays_only | Explicit true/false preserved; absent + frequency present → false; both absent → true | RoutineItem.from_dict |
| Routine start_on | Persisted created_on when absent/null | normalize_schedule |
| Routine weekdays | Anchor weekday when absent/empty | normalize_schedule |
| Routine month_day/year_month | Anchor day/month when absent/null | normalize_schedule |
| Routine schedule_history | Empty list (new field) | f7e622c dataclass default and rule_on |

Creation timestamps/dates are required. The old constructors could invent
today/now when absent; that is not reproducible evidence of a historical date,
so migration rejects such incomplete records. Invalid values are not treated as
missing values. Unknown domain fields, bad dates, duplicate IDs, bad references,
malformed recurrence and unexpected Schema8 history block migration.

Schema8 did not record old schedule changes. Empty history keeps the old meaning:
the current persisted rule interprets past and future dates. No earlier rule is
invented. Future edits use the existing exclusive `until` boundary; completions
are never regenerated. Reports explicitly state this historical information limit.

## Versioned pipeline and report

Raw bytes/hash → JSON parse → explicit Schema8 compatibility validator/converter
→ unchanged strict canonical validator → B3a atomic SQLite import/reopen/compare.
Conversion uses a deep copy, does not call tolerant Store.load and never writes
a Schema9 source file. Schema9 enters its original validator directly. Unsupported
Schema1–7/future inputs remain rejected. Invalid input creates no approved target.

Reports include source_schema_version, converter_version, applied_conversion_rules,
legacy_defaults_applied, historical_information_limitations, original_counts,
imported_counts, semantic_comparison, source_sha256, field_mapping_verified,
unsupported_legacy_fields, candidate_ready and switch_allowed=false. Arbitrary
JSON8 cannot prove which app saved it: source_app_version stays null. The tagged
fixture separately records its proven app version/provenance.

## Real-version fixture and independent checks

`tools/generate_schema8_fixture.py` exports the immutable tag commit with
`git archive` into TemporaryDirectory and runs its actual Python modules in a
subprocess with isolated APPDATA. It uses the old Store.save serialization path.
No real data is read. `tests/fixtures/schema8_v0102.json` is generated synthetic
data from genuine old code, **not a historical user's archive**.

It includes same-title/date distinct IDs, untimed multi-day skip-day/end-DDL,
timed reminders, native DDL, completion, inherited/override color, weekly habit,
todo, independent completions, leave/holiday, unknown/device preferences and
notification keys. The old code independently computes all Event occurrence
dates/endpoints/effective colors and 800 days of Habit planned dates. Tests compare
those results with reopened SQLite, not merely with a current-model conversion.

An additional test executes the tagged from_dict/load logic on partial records
and compares every returned field with our default conversion, including all
four workday-default combinations. Full fixture raw fields are also checked by
the canonical field comparison; settings/order/IDs/completions remain intact.

Tests cover aliases, missing/static/conditional defaults, old/new equivalent
semantics, history boundaries, source hash, conflict/rejection, source change,
existing target and rollback. All original B3a tests remain; the old rejection
loop now covers 1–7, with a dedicated Schema8 unknown-history rejection test.

## Limitations and B3b

Final validation: 30 new tests; 1191/1191 full tests passed (59.980 seconds),
including original Schema9 migration and dual Repository contracts. 153 Python
files compile; final changed files recompiled; diff/conflict checks clean.
HTTP negative-test resource warnings and intentionally duplicated ZIP-entry
warnings remain non-failing. Existing real Windows Tk isolated JSON smoke passed
modern/aero/paper/frutiger at 192 DPI: Compact/Flow/Timeline, move/resize/undo,
search and editor modal. No packaged EXE was built and no real data was migrated.

The old tolerant reader accepted some damaged/noncanonical input by coercion,
sorting, dropping or inventing values. This tool deliberately rejects uncertain
cases (including missing creation time, unsorted completion arrays, conflicting
aliases, mismatched inherited fallback color) instead of claiming byte-for-byte
restoration. Canonical v0.10 saved fixtures pass. Semantic tests cover finite
dates, not every possible future recurrence or every user file.

v0.10 backups use format_version=1, `manifest.json` plus `calendar_data.json`,
manifest schema_version=8 and integrity metadata. B3b must authenticate/validate
the container, extract read-only into isolation and use this versioned validator;
no backup format or restore code changes here.

Still required before activation: user-directory/process locks, old EXE gating,
immutable original backup, final hash under lock, recoverable atomic selection,
WAL-safe backup/restore and packaged upgrade failure tests. No B3b/B3c is enabled.

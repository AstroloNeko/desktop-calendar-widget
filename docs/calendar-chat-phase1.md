# Calendar Chat Service — Phase 1

Latest property adaptation and host execution policy supersede the original
confirmation-only baseline below: see [AI property adaptation](calendar-chat-attributes.md).

## Baseline and scope

- Based on `cb0c62b` / v0.10.2, calendar Schema 8 and backup format unchanged.
- Discarded Global redesign changes were explicitly withdrawn by the user, not merged into this feature. A local ignored patch preserves them.
- No dependency installation, version bump, Release, public listener, AstrBot/QQ code, or changes to calendar JSON/Event fields.

## Architecture audit and integration

`Store` owns JSON persistence, effective dates, custom leave/workdays, categories and effective color. Its lower-level mutation methods remain public for existing application code; they are **not** exposed to a model.

`EventCommandHistory.preview_upsert/preview_update/commit` provides detached previews, conflict detection, rollback on failed saves, stable IDs and session Undo/Redo. The new gateway uses this command path, not `Store.upsert` or JSON writes. Each confirmation is at most one event command. A no-op completion creates no history entry.

`CalendarApp._refresh_event_change` is reused to invalidate search/reminders, update Compact/Flow/Timeline and refresh day/DDL windows. `event_search.EventSearchIndex` and Store's effective dates are reused for bounded queries. Search/reveal and the existing editor remain unchanged.

All calendar reads and writes use the existing Tk main-thread queue through `TkDispatcher`. Workers do not touch widgets or create another Store. A timed-out queued dispatch is cancelled before it can later run; a dispatch already executing must be resolved by its operation receipt. UI calls run on daemon workers, never synchronously wait for HTTP or the service lock on Tk. Server workers and the desktop call the **same** service instance.

## Modules

- `calendar_chat_tools.py`: closed tool schemas and strict local argument validation, per-request real local time.
- `calendar_action_gateway.py`: Store/command boundary, effective-date preview, conflict fingerprints, commit and UI-refresh dispatcher.
- `calendar_chat_sessions.py`: protected SQLite WAL journal for sessions, request receipts, pending operations and audit metadata.
- `calendar_chat_service.py`: shared conversation orchestration, candidate selection, confirmation, cancellation, result and retry semantics.
- `llm_provider.py`: small HTTPS DeepSeek-compatible tool-call adapter and current-user Windows DPAPI protection. No SDK dependency.
- `calendar_chat_http.py`: opt-in authenticated IPv4 loopback adapter.
- `calendar_chat_ui.py`: lightweight themed chat/settings dialogs and lifecycle owner.

## Tools and conversations

Read-only tools: `list_categories`, `query_events`, `get_event`.

Write **preview** tools: `preview_create`, `preview_update`, `preview_complete`. `clarify` asks a question. No model tool can confirm, delete, batch-mutate, execute code, access files or set arbitrary user settings. Unknown fields/types/names fail locally; Event's legacy coercion is not used as validation.

Queries return up to 30 candidates and a true count/truncation flag. Date ranges include endpoints and are limited to 366 days; unfiltered queries default to this month, title-keyword searches without explicit dates search history. DDL-only queries use actual final effective dates. A category catalog contains at most 100 IDs/names, not event history. No full calendar or backup is automatically uploaded.

The session stores selected stable ID, candidates, clarified query/question and pending operation separately from text history. Multiple candidates cannot be auto-selected by a model. The user can click a candidate, reply with its number/exact title or a unique short fragment such as “实验室那个”. Subsequent updates merge into the same pending operation and retain the original fingerprint. Ambiguous “再提前半小时” is locally gated for clarification.

Every model request includes current system-local date/time/timezone and week boundaries. The current week/month/year is never derived from model training knowledge. Notes and creation/Snooze metadata are excluded from the pending context sent to the provider; model-generated free text cannot establish execution success. Query facts and confirmation UI are rendered from local structured results.

## Confirmation, conflicts and crash recovery

Pending operations have a random ID, session/principal binding, 15-minute lifetime, immutable target ID, original event fingerprint and effective-date preview. Every refinement changes the revision token. A stale UI/HTTP confirmation cannot execute a newly revised preview.

States: pending → confirmed → executing → succeeded, or cancelled / failed / expired. Conflicts require a new preview. Execution rechecks current event fields, category, reminder keys and effective dates through the existing command engine. A one-event commit preserves original Event IDs, native/last-day DDL, colors, reminder cleanup and Undo/Redo.

SQLite FULL-sync intent is committed **before** calendar mutation; a result receipt follows successful calendar persistence. SQLite and JSON are not claimed to be one distributed transaction. If the process stops between them, reading/confirming an executing operation compares the current event with the saved after-state. A match records a recovered success; a mismatch records `execution_uncertain` and **never automatically retries the write**. This conservatively sacrifices automatic retry if it cannot prove success. A manual edit/Undo/backup restore during this gap also prevents replay.

Message `request_id` receipts are durable and bound to principal and payload. A duplicate returns its original response; reusing it for different content fails. A processing receipt after a crash is not silently rerun. New identical text with a new ID is a distinct request. Repeated confirmations return historical results, not a claim that later Undo/edits never happened. Terminal receipts survive clearing chat; clearing chat cannot delete events.

## Configuration and privacy

Entry in Compact or Global: **更多 → AI 私人助理…**. This is a lightweight dialog, not a second calendar. The first implementation does not redesign Global's workspace into a drawer.

Settings let the user provide HTTPS base endpoint, model, own API key, timeout (5–60s), remote-data consent, and optional local API/port. Default endpoint is DeepSeek, the model name is editable for the user's account. A key and explicit privacy consent are required before any model request. A blank key entry preserves an existing key. No AstrBot/SnowLuma/private credential files are read. Redirects cannot forward the Authorization header to another host. Provider errors do not include bodies/keys/credential-bearing URLs.

`<current calendar data directory>/ai-assistant/configuration.dpapi` contains credentials, random HTTP token and preferences. `sessions.sqlite3` and its WAL contain DPAPI-protected payloads for the current Windows user. Audit payloads contain IDs/source/state/error category, not full notes or keys. Chat message IDs, request IDs and timestamps are recorded. These files are separate from calendar data and are **not included in existing calendar backups**. DPAPI is user/machine-bound, not portable backup encryption.

Limits: 50 active sessions, latest 100 messages per session, 6 model rounds / 18 total tool calls / 8 calls per response, at most 2 safe replans and a 90-second planning budget (provider socket timeout is capped to remaining budget). Multiple read calls are allowed; at most one proposal can be accepted for a request. A response with multiple proposals is rejected before any member is invoked, and must be replanned as a single-item intent or clarified. There remains one pending logical write per session, at most 10,000 journal records / 64 MiB main DB. When safety-receipt capacity is reached the service fails closed rather than pruning deduplication evidence. Ordinary calendar use continues. History clearing cancels pending operations but retains terminal/request receipts for safety.

## Local HTTP API v1

Default **off**. Enabling it in settings starts it with this application and resumes it on future application starts. It binds **only** `127.0.0.1`, default port 18746. Port conflicts are visible errors, not public-interface fallback. Main application exit stops the listener; there is no detached writer process.

All business routes require `Authorization: Bearer <token>`. Use the explicit **复制本机API Token** settings button; never place it in a URL, chat or public log. Host must equal `127.0.0.1:<port>`. Origin and browser Fetch headers are rejected; no CORS grant. Bodies are JSON, max 16 KiB, body socket timeout 5s, 8 concurrent connections, 120 requests/minute including invalid requests. A model response can take longer: clients should use a suitable response timeout and retry with the **same** request ID, not create a second operation.

| Method | Route | Body / purpose |
|---|---|---|
| GET | `/v1/health` | Public constant health/API version only |
| POST | `/v1/sessions` | `{}` → new session |
| POST | `/v1/chat/messages` | `{session_id, message, request_id}` |
| GET | `/v1/sessions/{sid}/messages` | History |
| GET | `/v1/sessions/{sid}/pending` | Pending/last operation |
| DELETE | `/v1/sessions/{sid}` | Clear chat, not calendar |
| POST | `/v1/operations/{oid}/confirm` | `{session_id, revision}` from the displayed preview |
| POST | `/v1/operations/{oid}/cancel` | `{session_id}` |
| GET | `/v1/operations/{oid}?session_id={sid}` | Durable operation result |

Response fields include request/session IDs, status, assistant_message, pending_operation, candidates, operation_result, error_code/error_message. Tools and previews are data, not executable code. Identities come from the adapter's bearer authentication; arbitrary `sender_id`/`is_owner` fields are rejected. Current tokens represent the single local owner; future multi-user/platform adapters need distinct authenticated principals/scopes, not trusting submitted QQ IDs.

Future AstrBot adapters can call these same endpoints, retain message idempotency IDs and display the exact operation/revision before confirmation. This phase provides no QQ plugin, remote access, Docker networking, unsolicited messages or platform trust verification. Another device/container's localhost is not this Windows process.

## Verification and limitations

Automated tests and GUI tools always use isolated Store files and Mock LLMs. No real user calendar, credentials or paid API calls are used. Run `python -m unittest discover -s tests -q`; opt-in Windows source/frozen GUI check: `tools/check_calendar_chat.py`.

Natural-language scenarios with Mock tools verify orchestration/state boundaries, **not** general real-model semantic accuracy. The orchestration/patch repair additionally passed two explicitly authorized real DeepSeek runs (12 total model calls) using fictional events and isolated Store/journal files, with the app's already configured credentials/consent read only. A/B/C passed, including a real response containing two read tool calls, and create → rename → time → date followups kept the same Event ID. Relative reminder ambiguity clarified without a write. This is bounded sample evidence, not a promise that every natural-language phrasing will be understood correctly.

Verification: 824/824 automated tests (780 baseline + 44 new); source and frozen QA each passed 8 four-theme × Compact/Global cases, with local confirmation, actual command/Undo, modal release, search reopening and loopback dispatch. The frozen run has an explicit completion report, not just exit code 0. The current physical Windows DPI is 192 (200%); this is not a claim of five physical monitor DPI tests.

The QA EXEs are built with existing Python 3.13.2 / PyInstaller 6.21.0 to ignored `build/calendar-chat-qa/`; no formal release artifact is created or replaced. A normal `app.py`-entry EXE also passed isolated APPDATA startup with persisted HTTP opt-in, authenticated session/history, safe missing-key response and listener shutdown on real application exit. The same CalendarApp/Store/commands/UI/HTTP classes run in the GUI QA entrypoint. A future formal release-package upgrade still needs release validation. No new runtime dependency or build-script modification is needed (SQLite and DPAPI are standard/in-box).

Provider format references: [DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/), [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/).

There is no AI Habit mutation, native notification/Snooze mutation, event deletion, batch AI operation, standalone always-on server, streaming UI, full conversation search, or model-independent natural-language parser. Calendar habit/reminder behavior remains on existing code paths.

## Orchestration and patch repair

- Old `len(tool_calls) != 1` rejection incorrectly conflated multi-tool reading with batch writes. The service now preflights the complete response (protocol IDs, names, strict arguments, proposal count), executes read dependencies first, then accepts at most one proposal. Every well-formed call ID has a corresponding result/error; malformed unexecuted envelopes are not replayed. Reasoning content is preserved when provided. The current official schema did not document `parallel_tool_calls`, so no unsupported parameter is relied on.
- Repeated read calls reuse request-local results. Two rounds without progress, round/call ceilings or planning timeouts stop processing. Recoverable argument/plan failures get bounded replanning; they never execute partial proposals. Failed planning retains the previous session target and pending card. Explicit “刚才” references and clarified choices keep the trusted target ID through model searches.
- `calendar_chat_intent.py` conservatively scopes optional fields to this user turn, or to a prior clarified intent. Renaming does not authorize fields merely mentioned inside the old/new title. Model-filled unrelated fields are omitted and recorded by **field name**. Unknown fields/invalid types still fail strict validation. Unclear phrasing is clarified instead of guessing a destructive patch; this scope guard is not a general NL parser.
- New events use `Store.make_quick_event` defaults. Unclassified new proposals map an unsolicited inherited-color default to no override; stored unclassified events still use legitimate override/default semantics. Category inheritance and explicit color overrides retain Store's existing rules. Updates remain detached partial patches; a title-only patch no longer rewrites due timestamps or removes seconds. Time/date are touched only when actually patched.
- Previews retain internal full Event snapshots for reversible commands, plus requested/changed-field metadata. New-event UI omits unrelated default fields. Error detail appears once in the conversation; the status bar gives a brief result and Confirm/Cancel tracks the actual preserved pending state.
- Protected journal `diagnostic` records are keyed by the hashed request identity. They contain tool names/counts/read-vs-proposal classification, safe field paths, omitted field names, cache/repair/error status, target-known flags and before/after pending status. They contain no argument values, titles, notes, raw messages, API key, HTTP token or reasoning. `get_diagnostics` is an owner-scoped developer interface, not a public HTTP route. Actual executions retain the existing separate audit receipts.
- Regression: 857/857 total tests (824 previous + 33 new), four themes × two views at real 192 DPI, UI batch-error/preserved-card checks and HTTP shared-service confirmation. Live metadata reports stay under ignored `build/calendar-chat-fix/`; no real calendar or chat history is used.

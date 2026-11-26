# Desktop Calendar Engineering Guide

## Repository architecture

- `app.py` and the Windows launchers compose the Tkinter desktop client. `desktop_calendar/global_view/`, `desktop_calendar/ui/` and `desktop_calendar/reminders/` hold reusable presentation, interaction and reminder components. Keep changes to the shared `app.py` local and inspect its diff carefully.
- `calendar_core.py` owns the calendar model and rules. `calendar_event_service.py` and `calendar_mutation_service.py` are application service boundaries. `event_commands.py` coordinates event history and Undo. `calendar_runtime.py` composes the same services for headless use.
- `desktop_calendar/storage/` owns the Repository contract, SQLite and legacy JSON implementations, and storage authority, migration and recovery. `desktop_storage_startup.py` selects the desktop startup path. `desktop_backup.py` connects backups and restore to that boundary.
- `desktop_calendar/assistant/` owns Calendar Agent orchestration, the LLM provider, Operation Plan, Pending and the separate ChatJournal. `calendar_chat_http.py` within that package exposes the local HTTP API. The `desktop_calendar/remote_*.py` modules connect the desktop Worker to the cloud and handle QQ and WeChat channels through the runtime.
- `tests/` contains offline unittest coverage. `tools/windows_tests/` and selected `tools/check_*.py` scripts cover native acceptance. `build_release.ps1`, `build_native_notifications.ps1`, `updater.py` and `repair_launcher.py` are Windows build and update entry points.

## Development rules

- Treat the current repository code as the source of truth. Inspect and report the branch, HEAD and working-tree status before edits. Preserve unrelated changes and keep the default branch releasable.
- Push, merge, tag, release or perform destructive Git operations only with explicit authorization. Follow the scope of any authorization already given.
- Preserve compatibility with user data and settings. Reuse existing components and interfaces, avoid unrelated refactoring and obtain approval before adding a necessary dependency.
- Report the files changed, checks actually performed and remaining limitations. Base any expansion of scope on a concrete risk, dependency or failure. Avoid whole-repository audits, redundant checks, lengthy incidental reports and wrapper layers for small tasks. Leave unrelated technical debt outside the task.

## Architecture and data safety

- Keep business rules in Calendar Core and Application Services. GUI, Agent, HTTP and Remote Worker are adapters to those boundaries. Keep rendering and theme tokens out of business logic.
- SQLite is the default for new profiles. Use the storage lifecycle for legacy JSON compatibility and explicit upgrade. An activated SQLite profile must never silently revert to JSON.
- Write through the Repository and its transaction boundaries. Keep committed state, operation receipts and Undo consistent. Schema changes require deliberate approval and compatibility review.
- Use synthetic fixtures for destructive tests. Require explicit consent for migration, overwrite or deletion of user data. Preserve unrelated preferences and the separate AI journal and remote bindings.

## Calendar Agent

- Use the LLM for natural-language interpretation and structured plans. Use deterministic code for retrieval, validation, confirmation, execution and verification. Fix deterministic bugs in code.
- Reuse Operation Plan and Pending. Revalidate target identity and revision before executing, reject stale plans, and ensure repeated confirmation cannot apply a change twice. Report success only after the outcome is verified.
- Use scripted providers, synthetic data and test clocks for regression. Keep real model calls bounded and restricted to authorized Live acceptance. Reuse the current orchestration for straightforward changes.

## Theme lifecycle

- Active themes are **Modern** and **Slate**. FA (Frutiger Aero, `frutiger`), Glass (Windows 7 Glass, `aero`, alias `win7_aero`) and Paper (`paper`) are archived. Preserve their source and resources. Exclude them from the user menu and routine visual, layout and GUI testing.
- Historical IDs remain readable. These light palettes resolve to Modern at the desktop boundary without rewriting the saved ID on load. The historical `gemini_reviewing` alias resolves to Slate. Maintain this compatibility with small offline tests.
- Use semantic tokens from `desktop_calendar/ui/ui_theme.py` and shared controls and Canvas helpers. Test the changed component in Modern when presentation verification is needed. Include Slate when shared tokens or visuals are affected and for relevant release acceptance. Reactivation requires restoring the registry entry to active use, updating compatibility policy, adapting the UI and running targeted acceptance.

## Testing policy

**Automated tests must not interfere with the developer's normal desktop usage.**

**Do not run the entire test suite by default. Select tests based on the actual change and its risk.**

**Archived themes are excluded from routine visual and GUI testing.**

Run from the repository root. See [testing commands and acceptance requirements](docs/testing.md).

| Level | Scope | Entry point |
| --- | --- | --- |
| L1 Targeted Tests (default) | Changed behavior | `py -3 tools/run_tests.py -p test_ui_theme.py -k main_menu` |
| L2 Module Regression | Affected module and direct dependencies | `py -3 tools/run_tests.py -p test_ui_theme.py -p test_ui_draw.py` |
| L3 Full Offline Regression | Release, major shared changes or explicit full regression | `py -3 tools/run_tests.py --all` |
| L4 Windows / Live Acceptance | Selected native behavior, packaged EXE, real API or remote service | `py -3 tools/run_tests.py --windows --allow-desktop` or the specific acceptance command in the testing guide |

Use `--list` to inspect unittest selection without running cases. Expand L1 only when interfaces, direct dependencies, user data or compatibility warrant it. Report Passed, Failed, Skipped, Not run, Manual review required and Archived separately. Never count unexecuted checks as passed or relax assertions to hide a failure.

Offline tests use isolated APPDATA, temporary Stores and journals, scripted providers, mocks for GUI/Windows boundaries and loopback HTTP where needed. They must not create visible windows, change focus or other windows, send real mouse or keyboard input, open native dialogs or file pickers, register global hotkeys, send desktop notifications, launch the installed calendar, change system settings, access real user data, call a real model or reach production services. Close repositories and database handles, stop servers and threads, cancel timers and clean up only processes owned by the test.

Keep native assertions in L4. Explain the specific effects and obtain authorization before any real desktop interaction or Live call. Scope it to the requested acceptance, even during a release. Use isolated data and credentials and a disposable Windows session for packaged tests that rely on shared operating-system resources. Report native and offline results separately.

# Desktop Calendar Project Rules

## Repository structure

- `app.py`: main window, UI interaction and application coordination
- `calendar_core.py`: calendar settings, storage and business logic
- `ui_theme.py`: semantic tokens for Modern and Win7 Aero themes
- `ui_draw.py`: reusable Canvas drawing helpers
- `tests/`: automated tests
- `build_release.ps1`: Windows release build
- `version.py`: application version

## General rules

1. Treat the current repository code as the source of truth.
2. Before making changes, report:
   - current branch
   - working-tree status
   - latest commit
3. Keep `main` stable and releasable.
4. Do not push, merge, tag, create a release or delete branches without explicit approval.
5. Do not install new dependencies unless clearly necessary and approved.
6. Do not refactor unrelated code while completing a scoped task.
7. Preserve compatibility with existing user settings and data.
8. Run relevant tests after modifications.
9. Clearly report changed files, tests performed and remaining limitations.

## UI rules

1. The application supports `modern`, `win7_aero` and `paper`.
2. New UI must work correctly in all themes.
3. Do not hard-code colors, fonts, borders or visual state values in `app.py`.
4. Reuse semantic tokens from `ui_theme.py`.
5. Reuse Canvas helpers from `ui_draw.py`.
6. UI changes must not alter calendar, storage, reminder or update logic.

## Business logic rules

1. Keep date calculation, storage, settings and reminder logic separate from visual rendering.
2. Business modules must not depend directly on theme colors or Canvas drawing.
3. Add or update tests for observable behavior changes.
4. When new UI state is needed, expose the smallest reasonable interface.

## Shared-file warning

`app.py` contains both UI coordination and event handling. Avoid large unrelated edits and inspect its diff carefully before merging.

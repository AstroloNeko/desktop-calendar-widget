# v0.9.2 UI polish and acceptance

Baseline: `80d6658` (Bulk Actions), containing `b063b7f` (Habit 2.0), on top of
v0.9.1 `5904ae5`. UI branch: `ui/v092-final-polish`.

## Changes

- Habit editor: compact weekday toggles, explicit selection/hover colors,
  consistent dropdown fonts and disabled date inputs, themed history scrollbar.
  A live rule summary uses the existing validated values and `schedule_label`;
  invalid intermediate input shows guidance rather than a different rule.
- Multi-selection: thin rounded outlines for task segments and fine fixed-column
  outlines. Category stripes and deadline markers remain visible. No filled
  selection layer covers task text.
- Bulk toolbar: a single quiet accent surface, emphasized count and separator,
  lighter secondary buttons, explicit complete/uncomplete actions and danger text.
  Its existing visibility threshold and position are unchanged.
- Category popup: category-color swatches without claiming a shared current
  category for mixed selections. Windows retains its native popup behavior.
- Date adjustment: themed input/focus and existing theme buttons, with live
  “提前 / 延后 N 天” preview. Signed input and transaction validation are unchanged.
- Count-aware nonmodal feedback and clearer second deletion confirmation. Both
  confirmations and one-step Undo remain the existing behavior.

No Task model, recurrence matcher, date/DDL calculation, storage, command engine,
backup, update, native-window integration or DPI implementation is changed by
the polish. Existing tokens and Canvas helpers are reused; no dependencies or
image assets are added. Completion history is not reclassified or erased.

## Evidence

- Baseline full suite: 551/551. Polished suite: 553/553, including two new UI
  wording tests. Python 3.14 and the existing release Python 3.13 both passed.
- `check_habit_schedules.py`: 40 theme/view/DPI editor cases, all cycle choices,
  save/reload/history, owner/grab cleanup, manager/day-detail ownership and Ctrl+K.
- `check_bulk_actions.py`: all eight theme/view cases; modifiers, single-item
  drag behavior, completion, category/color inheritance, date validation, deletion
  cancellation, atomic Undo/Redo, filter/month/view changes, five DPI layouts.
- `check_v092_visuals.py`: 20 theme/DPI cases with 5–10 selected tasks, 720/1000
  logical-width toolbars, weekday bounds, summary and date-dialog screenshots.
  Real Windows deletion dialogs exercise cancellation at either step and actual
  deletion followed by one Undo; persisted event facts and modal cleanup match.
- `check_backup_gui.py`: 16 theme/view/pin cases pass backup/restore with no
  callback errors. 100 habit rules survive a separate backup/restore roundtrip.
- Local performance samples: 100 rules across 365 days ~0.025 s; GUI 500 tasks
  selecting 10/50/100 takes ~0.327/0.403/0.548 s including layout settlement;
  complete+Undo+Redo ~0.232/0.275/0.320 s. Settled selection does not rebuild the
  active renderer. These are samples, not timing guarantees.

Native monitor DPI is 192 (200%). 96/120/144/168 are layout simulations through
the existing DPI manager, not claims of changing physical display settings.
Isolated screenshots are ignored under `build/v092-ui` and `build/habit2-smoke`.
PrintWindow can render native Entry/checkbox details differently from desktop
composition; task text, bounds, saved state and actual modal ownership are
checked separately. The native category menu smoke now invokes its registered
command inside the popup loop and ends that QA thread's menu loop; it no longer
waits for an external click before reaching the next assertion. This is test
machinery only, not an application-window workaround.

Release/packaged-binary evidence is recorded separately during the release phase.

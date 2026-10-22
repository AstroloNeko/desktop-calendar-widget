# v0.9.0 final visual polish

Branch: `ui/v090-final-visual-polish`.
Base: `da1be4b1bd2efa38753aa672394ea62e8c109ce6` (committed frameless drag fix).

Changed files: `app.py`, `global_header.py`, `global_drag_ui.py`,
`global_search_ui.py`, `search_palette_ui.py`, `ui_theme.py`, `ui_draw.py`,
`tests/test_ui_draw.py`, `tests/test_window_behavior.py`,
`tools/check_final_visuals.py` and this report.

## Visual changes

- Global header: slightly smaller month title and title reservation, consistent
  vector navigation/minimize icons, quieter secondary actions, a fine bottom
  edge and a narrow glass reflection for Glass/Frutiger. Existing native move
  sessions and button commands are unchanged.
- Workspace: semantic sidebar/detail/grid surfaces, shared padding and gaps,
  unboxed category navigation, quieter separators. Detail width now uses the
  correct `pack_propagate(False)` for its packed children; the existing
  responsive width/hide policy is preserved. A rebuilt inspector resets its
  cached visibility to match the newly gridded widget, so switching themes at
  narrow widths cannot accidentally expose it and squeeze the timeline.
  Focus border colors are explicit, preventing Tk's default black edge during
  keyboard focus or a scheduling drag.
- Flow: keep the 28-logical-pixel cards and packing density. Title and metadata
  use independent centered anchors (8/22) instead of overlapping top/bottom
  anchors. Actual Chinese glyph bounds were checked at all five scales.
  Category stripes, task nature, Today, selection and deadline outlines retain
  separate channels. Cross-day dividers are quieter.
- Timeline: keep the existing virtualized renderer and shared month model.
  Fixed-column and segment titles elide by measured font width. Bar text uses
  the more readable theme foreground for arbitrary user colors. Native DDL no
  longer repeats its label as “DDL · DDL”. Grid lines are lighter. Empty-state
  text is anchored to the visible viewport, including horizontal scrolling,
  rather than the center of the full month canvas.
- Resize/drag: true endpoint hit regions are unchanged. Persistent grips appear
  only on the selected task, with a thin category-toned stroke; hover exposes a
  small rounded grip. Pooled ghost targets use a light category tint and dashed
  rounded edges; source dimming, deadline marker, cancel and command transactions
  remain the existing implementation.
- Search: inset field with vector magnifier and focus edge, compact heading,
  bottom keyboard/count hint, themed scrollbar, fine selected/hover outline,
  readable keyword backgrounds, neutral completed color stripes. Matching,
  ranking, result order, cap, activation and temporary reveal are unchanged.
- Tooltips share padding, wrapping, border and DPI scaling. Canvas tooltips use
  their normal transient owner and work-area clamping instead of permanent
  topmost. Compact receives only these shared tooltip improvements.

Theme additions are `workspace_sidebar`, `workspace_detail`, `workspace_grid`,
`search_match_background` and `search_match_text`, with small shared metric
tokens for spacing, Flow anchors, Timeline font sizes and tooltips. Modern is
warm-neutral, Glass silver-white, Paper cream with a brighter inspector, and
Frutiger aqua with a pale mint-white inspector. No new content-color constants
were added to `app.py`.

## Validation

- Full automation: **414/414** (baseline 413). Updated detail assertions verify
  the same facts in their new visual locations; the additional test verifies
  readable bar text for light/dark user colors in all four themes.
- Python compile, diff whitespace, conflict marker, new `app.py` hex-color
  check: passed.
- `tools/check_interactions.py`: all four themes, Compact/Flow/Timeline,
  move/start-resize/end-resize/cancel, undo/redo, search keyboard activation,
  modal grabs, editor save and temporary filtered reveal: passed in real Tk.
- `tools/check_final_visuals.py --phase after --dpi 96 120 144 168 192`:
  real Tk renderings, glyph bounds, header fit, narrow/wide layouts, Compact
  Detail, Global Editor, Category Editor and search. Category Editor is opened
  ten times per theme at native DPI, with text insertion and grab cleanup.
- Native hardware DPI is 192 (200%). Other values are layout simulations using
  the existing DPI path, not changed Windows monitor settings.
- Empty Timeline: all four themes, five scales, three window widths and three
  horizontal positions (180 checks) retain centered, unclipped text.

The baseline and final PNGs are under ignored `build/final-ui-qa/before` and
`build/final-ui-qa/after`. Each final theme/scale has Flow, Timeline, Search and
Compact captures (80 main PNGs, plus eight native-scale drag previews), with a
results manifest. They are rendered
from isolated temporary data, via PrintWindow on the actual mapped HWND.
Native non-client margins can appear black in PrintWindow; these captures do
not validate desktop compositing or window-move trails. Some native Entry text
also prints smaller than its actual desktop rendering; native 192-DPI font
metrics remain correct (9 points, 31-pixel line spacing). After the desktop was
unlocked, actual desktop captures (`qa_frameless_*.png`, ignored) showed clean
window edges without those PrintWindow margins. Empty-state regression captures
are under `build/final-ui-qa/empty-viewport`.

Final native 200% desktop captures with the isolated task/category fixture are
under `build/final-ui-qa/native-desktop`: Flow, Timeline, Search and Compact in
all four themes (16 screenshots). These show the real native controls rather
than their PrintWindow fallback rendering.

Native-scale drag previews for both Global modes are additionally captured
under `build/final-ui-qa/drag-check`, with clean cancel/grab restoration checks.

## Performance samples

Same fixture/tool, native 192 DPI; milliseconds, before → after. These are local
single-run samples, not timing guarantees. One concurrent-QA 1000-item sample
was slower; the isolated rerun below returned to baseline range.

| Path | 100 tasks | 500 tasks | 1000 tasks |
| --- | ---: | ---: | ---: |
| Flow render | 38.82 → 34.82 | 72.41 → 45.26 | 76.50 → 77.83 |
| Timeline render | 26.72 → 27.23 | 41.79 → 45.12 | 51.76 → 52.46 |
| Month change | 107.23 → 96.61 | 118.90 → 105.61 | 117.95 → 103.10 |
| Category filter | 114.24 → 104.36 | 129.94 → 125.29 | 142.80 → 136.08 |
| Search visible | 101.07 → 95.72 | 94.07 → 104.10 | 97.77 → 95.83 |
| Drag frame median | 9.65 → 6.51 | 10.21 → 9.82 | 10.21 → 9.53 |
| Drop to paint | 48.75 → 39.94 | 62.00 → 58.77 | 73.24 → 74.71 |

## Native desktop acceptance

After unlocking and exposing the taskbar, a full uninstrumented
`check_frameless_global.py --taskbar --screenshots` run passed: all four themes,
native editor/messagebox Z-order, Compact Detail, search, pin state, 20 shell
taskbar restores, native move/resize hit tests, Alt+Tab/Alt+F4 and three exact
geometry reloads. The HWND and renderer objects remained stable, and the
native caption never reappeared.

`check_window_drag.py --dpi-matrix` also passed all 20 real native drags
(four themes × five layout scales), checking exact displacement, session/grab
cleanup and exactly one position save per move.

`check_window_drag.py` passed the desktop-trail capture run for Compact and all
four Global themes. Each gesture has 49 pointer positions and three in-motion
captures. Client black-pixel samples were zero; after release, both client
black samples and background-residue samples were zero in every case. PNGs and
the raw results are under ignored `build/drag-qa`. In-motion background counts
also include the OS Snap overlay/compositor latency and are not alone evidence
of a rendering trail; client samples and settled desktop captures are checked
separately.

Earlier repetitions failed while the desktop was inaccessible or another
window covered the taskbar. Traces recorded no native press/NULL foreground;
the taskbar guard refused clicks on the covering window. One early repetition
failed the tooltip focus invariant. These attempts were not counted as passes.
The final complete run above passed the original assertions, without changing
the native QA script or production window management to bypass the failures.

No Task/Category model, schema, date/DDL/skip rules, command transactions,
search engine, persistence, native window integration, DPI implementation,
updater, backup, license, version or build logic was changed. This change is
delivered as a local-only UI commit; no push, merge, tag or release is included.

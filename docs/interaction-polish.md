# v0.9.0 interaction polish (UI phase)

Base: `0a307e6` on `feature/v090-interaction-engine`; UI branch:
`ui/v090-interaction-polish`. Baseline: 359 tests. Release version unchanged.

## Scope

No changes to Event/EventCategory, schema, dates, skip days, DDL rules, color
inheritance, command transactions, search matching/ranking, storage, DPI APIs,
updater, build or licensing. `interaction_visuals.py` and `search_palette_ui.py`
are presentation-only helpers; both Global renderers still consume the same
immutable month model and the existing schedule preview API.

## Scheduling and feedback

- Muted source placeholder, compact pointer-adjacent date bubble, shallow date
  header snap highlights and independent DDL marker. Content colors remain
  content colors. Aero/Frutiger add a restrained highlight; Paper uses the
  existing paper surface. No large translucent date-cell fill or animation.
- True endpoints alone expose hover grips and resize cursors; weekly/clipped
  segment boundaries do not become endpoints. Scroll uses current Canvas
  coordinates and the existing schedule calculation, not a second algorithm.
- Preview Canvas items are pooled in bounded, viewport-indexed slots. Cancel,
  invalid targets, month changes and view/theme changes restore source styles
  and clear date-axis overlays. Blank-date selection now updates a small range
  overlay rather than rebuilding Flow on every mouse motion.
- One replaceable 2.2-second notice occupies the existing status/footer strip.
  It is a Label, not a window: no grab, topmost, focus stealing or messagebox.
  Creation/edit/completion/deletion/categories/movement and undo/redo use it.
  Search targets and successful drops receive a brief outline emphasis.

## Palette

The moderate-size panel retains the existing modal owner stack. Results have
separate title, category/date, and quiet DDL/urgent/completed semantics. Group
headers preserve the order produced by the unchanged search service; no
additional sorting happens in the renderer. Visible keyword spans are
highlighted without altering search relevance or Unicode content.

The Canvas list draws only visible rows, keeps keyboard selection visible,
tracks hover separately, elides text by measured font width and reflows on DPI
changes. Prior results stay dimmed during a 45 ms debounce instead of flashing
empty. Mouse actions cannot execute stale results; Enter during a pending query
executes the current result after delivery. Empty/no-match/error states are
explicit. The original 100-event cap and temporary reveal/filter policy remain.

Close cancels polling/debounce/deferred activation, removes the variable trace,
and explicitly releases query variables/fonts on the UI thread. Thirty rapid
open/close cycles retained exactly one application Tk variable before/after.

## Renderer stability

Timeline draws only the visible row window plus three overscan rows. Scrolling
replaces that layer, not the fixed date axis or month model; the full logical
scroll region and fixed-name-column synchronization remain. Flow projection is
lazy while Timeline is active, and is reused across view switches until the
month model changes. Configure redraws are coalesced over 32 ms and cancelled
when a synchronous draw already fulfills them.

Canvas tag dispatchers update their callback target instead of registering new
Tcl commands on each repaint; bindings for departed tags are pruned. This
prevents callback accumulation across month/filter/viewport changes.

## Native performance (milliseconds)

Same Modern fixture, temporary Store, actual 192 DPI, 100/500/1000 events.
Each cell is **before → after**. These are local samples, not timing guarantees.
Search includes debounce, worker delivery and visible list paint.

| Path | 100 | 500 | 1000 |
|---|---:|---:|---:|
| Flow render | 60.20 → 39.23 | 85.46 → 48.95 | 86.72 → 61.83 |
| Timeline render | 90.81 → 28.84 | 248.45 → 33.16 | 503.93 → 41.51 |
| Month change | 122.39 → 93.89 | 99.51 → 87.90 | 131.89 → 84.35 |
| Category filter | 147.02 → 97.44 | 123.18 → 109.57 | 153.74 → 114.45 |
| Search visible | 109.84 → 84.27 | 113.73 → 92.48 | 115.21 → 93.34 |
| Drag frame median | 11.67 → 8.18 | 9.16 → 8.61 | 11.02 → 8.08 |
| Drop to paint | 76.94 → 35.43 | 220.29 → 46.06 | 471.91 → 54.96 |

The table uses the final full-smoke benchmark run. Earlier polish runs measured
9–11 ms preview frames and a 54–74 ms 1000-item drop, illustrating normal timing
variation. Flow packing/filtering still needs one full view paint; it has not
been rewritten or virtualized. These results do not promise a fixed frame rate.

Reproduce: `py -3 tools/benchmark_ui_polish.py`. All fixture data is temporary.
`py -3 tools/check_interactions.py` checks four themes, move/resize/cancel,
undo/redo, search/keyboard/editor save, modal grabs and temporary filter reveal.
Additional native checks exercised bottom-of-list virtualization, fixed columns,
stable callback counts, repeated Compact detail/editor/search chains, and five
layout scale simulations (96/120/144/168/192); actual hardware DPI was 192.

## Remaining limits

- No animations, drag task bars between projects, or new scheduling semantics.
- Search still shows at most 100 event matches; refine the query for more.
- Full month/filter changes and committed Flow lane repacking are not free.
- DPI ratios other than 200% were simulated through the existing layout path;
  this run did not change the operating system's physical display settings.
- Fine visual refinements beyond these existing semantic theme surfaces remain
  optional, not a new independent theme system.

# Visual Deck Plan — Moxfield-style stacks, edit in place, Shortlist alongside

Working plan from the review of 2026-10-12, following the finished
[UI_ACTION_PLAN.md](docs/archive/UI_ACTION_PLAN.md). Goal: manage a deck while looking
at the cards, the way Moxfield does — cards in overlapping stacks grouped
by type, hover for the full card, and quick edits without leaving the view.

Each phase ships on its own: the app runs, tests pass, and nothing is
half-converted at a phase boundary. Do them in order — Phase A builds the
component read-only, Phase B adds writes to it, Phase C adds the Shortlist.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done

- [x] **Phase A** — Read-only Stacks view (custom component). Done 2026-10-12: payload ~29–36 KB for 73–91-card decks (real DB, under the 50 KB target); JS not yet eyeballed in a browser.
- [x] **Phase B** — Edit in place on the board. Done 2026-10-12: 🃏 Board tab (card menu, add bar, stats strip, one-level Undo) on `deck_board.apply_board_action()`; full-page rerun kept (measured ~0.2s). JS menu not yet clicked through in a browser.
- [x] **Phase C** — Shortlist alongside the deck; pair and apply swaps visually. Done 2026-10-12. Python side started 2026-10-12: `plan` / `unpair` / `apply` ops in `apply_board_action()` (apply matches `writes.apply_changes()` table-for-table, tested), and the Shortlist payload (`load_board_shortlist()`, verdicts allocated across every deck; largest real Shortlist ~10 KB, so board + Shortlist stays under ~50 KB) plus `cut_for` badges, passed to the component as `data.shortlist`. JS side: Shortlist in a sticky right rail above the preview, pick mode, two-click Apply — clicked through in headless Edge on a DB copy (plan / unpair / apply round-trips, Esc / cancel, commander not pickable). Name-only planned swaps (all 44 real ones, backfilled from the old queue) now show too; `apply` links their printing like the Shortlist tab. `drop`, `add` (no cut) and their Undo (`reopen` / `to_shortlist`) added; Drop/Undo/Add and plan → Workbench → apply → Workbench checked in headless Edge on a DB copy (the Workbench lists a Board-planned swap and stops listing it once the Board applies it). Docs updated. C2 drag-and-drop not built (optional; click-to-pair hasn't been used enough to judge).

---

## Why a custom component (and not native widgets)

Image payload is no longer the constraint: since UI plan Phase 4, art is
served from `static/` by URL (a 48-card grid is ~24 KB of page content
per rerun) and the browser caches it. The constraint is Streamlit's rerun
model: every widget interaction reruns the script. A native version — a
`st.image` + `st.popover` per card — means ~100 popovers and a rerun on
every click, which feels slow and cluttered.

A **Custom Component v2** (`st.components.v2.component`, present in the
pinned Streamlit 1.63) moves the interaction into the browser:

- **Python → JS:** the deck goes in as `data=` (a list of plain card dicts)
  on every run.
- **In the browser, free:** layout, regrouping, hover preview, menus.
  None of these rerun the script.
- **JS → Python:** only a committed edit comes back, as a trigger
  (`setTriggerValue("action", {...})`) — one rerun per real change.
- **UI-only preferences** (group-by, sort) persist with
  `setStateValue(...)`, which survives reruns without one being needed.

CCv2 renders in a shadow root under the page (no iframe), so the
component can read Streamlit's `--st-*` theme variables and match the
app's light/dark theme.

**Inline, not packaged.** A packaged component needs Streamlit's official
`component-template` (npm, Vite, a `pyproject.toml` manifest). That's
more tooling than one view needs. Instead the JS and CSS live as real files
in the repo — `dashboard_lib/components/deck_board.js` and `.css`, so they
get editor syntax highlighting and diffs — and Python reads them at import
time and passes them as **multi-line strings**. (CCv2 treats a
single-line, path-like `js=`/`css=` string as a packaged-asset reference
and errors without a manifest; a file's contents are always multi-line, so
this is safe.) No npm, no build step, plain DOM JS. Graduate to a packaged
component only if the JS outgrows one file.

---

## Phase A — Read-only Stacks view

Prove the component, the data contract and the look, with no write risk.

### New files

- **`dashboard_lib/deck_board.py`** — Python side. Declares the component
  **once at import** (re-declaring per call confuses CCv2) and exposes:
  - `board_payload(cards_df, deck_meta, owned_qty)` — **pure**, no
    Streamlit: turns the deck dataframe into the list of card dicts the JS
    gets. Unit-testable.
  - `render_deck_board(cards_df, deck_meta, *, key, editable=False)` —
    calls `board_payload()` and mounts the component. The page never
    touches `data=` or callback wiring directly (the CCv2-recommended
    wrapper pattern).
- **`dashboard_lib/components/deck_board.js`** — `export default
  function(component)`, rendering under `parentElement` (never
  `document`, so two instances can't collide) and returning a cleanup
  function. CCv2 APIs only: no `Streamlit.setComponentValue`, no
  `window.parent.postMessage`, no `streamlit-component-lib`.
- **`dashboard_lib/components/deck_board.css`** — uses `--st-*` theme
  variables for text, background and accent colors.

### Data contract (one dict per mainboard card)

From `loaders.load_deck_cards_df()` (already `CARD_COLUMNS` +
`quantity` + `strategy_tags`):

| key | source |
|---|---|
| `id` | `scryfall_id` |
| `name`, `qty`, `cmc`, `mana_cost`, `type_line` | as stored |
| `img` | `card_view.card_image_src(row)` — the static URL, Scryfall fallback |
| `type` | `formatting.derive_card_type()` (front-face bucket, as elsewhere) |
| `is_land` | `formatting.has_land_face()` (MDFC-aware) |
| `colors` | `color_identity` letters |
| `tags` | `strategy_tags` split to a list |
| `flags` | `game_changer`, `reserved`, `not_owned` (no lot for its oracle_id, via `loaders.load_owned_card_quantities`), `commander` (name matches `decks.commander` / `partner`) |
| `price` | `current_price_usd` |

Never send a local filesystem path or a base64 image — URLs only, so the
payload stays at Phase 4's size (target: under ~50 KB for 100 cards;
measure it with AppTest the way Phase 4 did).

### Layout and behavior

- **Commander stack first**, always, then one column-stack per group.
- **Group by** (inside the component, switches instantly):
  Type (default — Creature, Instant, Sorcery, Artifact, Enchantment,
  Planeswalker, Battle, Land, in that order), Mana value, Strategy tag,
  Color. Saved via `setStateValue("group_by", ...)` so it survives reruns.
- Within a stack, sort by mana value then name. Each stack header shows the
  group name and card count (counting quantity — 30 basics count as 30).
- **Stacks:** cards overlap so only the top ~14% (name bar) of each shows;
  the last card in a stack shows in full. Columns wrap to fit the width.
- **Hover** (tap on touch screens) shows the full card in a fixed preview
  panel beside the board, plus name, price and tags. Cached art is
  Scryfall's `normal` size (488×680), so one image serves both stack and
  preview — no second fetch.
- **Badges** on the card's name bar: `×N` for quantity > 1, a Game Changer
  marker, and a "not owned" marker. Reserved List goes in the preview only.
- Images use `loading="lazy"`.
- Clicking a card does nothing in Phase A (Scryfall link lives in the
  preview panel).

### Where it appears

Decks page → **Cards** tab: the browser's `View` control gains a third
option — `Table` / `Image Grid` / `Stacks` — when the board choice is
**Mainboard**. Stacks ignores the browser's own group/sort controls
(it has its own) but respects the 🔍 Filters expander, so "show me only the
Removal-tagged cards as stacks" works. Because `render_browser()` is
shared with the Collection page, add Stacks as an opt-in argument
(`stacks=False` by default) rather than for every caller.

### Tests

- `tests/test_deck_board.py` — `board_payload()`: commander flagged and
  pinned first, `type`/`is_land` correct for an MDFC land, `not_owned`
  set from the owned map, no `local_image_path` or `data:` URI in the
  output, payload size for a 100-card fixture deck.
- `tests/test_pages_smoke.py` — Decks page renders with View = Stacks
  for a normal deck, an empty deck, and a deck with no cached images.
- AppTest can't click inside the component's JS, so interaction is
  checked by hand in the running app; keep logic out of the JS where
  possible so the Python side carries most of the testing.
- The offline harnesses under `scripts/_test_*` that fake `streamlit`
  need a `components.v2.component` stub if they import `deck_board`.

### Done when

The Decks Cards tab shows a deck as type stacks with hover preview,
regrouping is instant (no rerun), it looks right in light and dark theme,
and the payload size is measured and noted here.

---

## Phase B — Edit in place

Make the same component editable. Editing lives in the **Deck Editor**,
not the Decks page — the Decks page stays read-only, matching how the
app splits viewing and editing today.

### Where it appears

A new first tab on the Deck Editor, **🃏 Board**, mounting
`render_deck_board(..., editable=True)` inside an `st.fragment` along with
its add bar and stats strip, so an edit reruns only that block.

### Actions (JS → Python)

A click on a card opens a small menu in the component:
**+1**, **−1**, **Remove**, **Move to Shortlist**, **Tags…**.
Each choice sends one trigger:

```
{"op": "inc" | "dec" | "remove" | "to_shortlist" | "set_tags",
 "id": "<scryfall_id>", "tags": [...]}   # tags only for set_tags
```

All ops map to existing write functions — **no new writes, no schema
change**:

| op | write |
|---|---|
| `inc` / `dec` | `writes.bulk_update_deck_cards(conn, deck_id, {id: qty ± 1})` (0 removes) |
| `remove` | `writes.remove_deck_card()` |
| `to_shortlist` | `writes.remove_deck_card()` then `writes.add_change(..., add_scryfall_id=id)` as an `idea`, so it can come back |
| `set_tags` | `writes.set_card_tags(conn, deck_id, id, "strategy", labels)` |

The handler is one **pure** function in `deck_board.py`:
`apply_board_action(conn, deck_id, action) -> {"message", "undo"}`. It
validates the payload (unknown op, missing id, card no longer in the deck
→ `ValueError` with a user-facing message, shown as `st.toast`) and is
what the tests exercise. The component callback just calls it and then
`loaders.invalidate_deck_caches(deck_id)`.

**Python wins.** The JS may update a quantity optimistically for
snappiness, but every rerun re-hydrates from `data=`, so the database is
always the truth. The handler reads current quantities from the database,
not from the payload, so a double-click on −1 is two correct decrements,
not a stale overwrite.

### Add bar

A native Streamlit row above the board, not inside the component: name
search over local cards with the Scryfall fallback, quantity, Add. This is
the Mainboard tab's existing add flow (`card_resolver.resolve_or_fetch_card`
→ `writes.add_deck_card` → `writes.auto_add_to_collection`). Pull it out
into one function both tabs call instead of copying it.

### Stats strip

Above the board, inside the fragment, so it updates with each edit:
card count vs. 100 (commander included, matching how `deck_cards` stores
it), lands, average mana value of nonlands, and a small curve bar. Compute
from the same dataframe the board uses — no extra query.

### Undo

One level. `apply_board_action()` returns the inverse action (`remove` →
re-add at the old quantity, `inc` → `dec`, `to_shortlist` → re-add and
drop the idea, `set_tags` → old labels). The page keeps it in
`session_state` and shows a "↶ Undo: removed Sol Ring" button next to the
stats strip until the next edit. (`st.toast` can't hold a button.)

Undoing a remove re-adds the card, and both are logged to the deck's
history as `applied` rows — history records what happened, including the
mistake. **Decided (2026-10-12): accept it** — undo never deletes history;
the re-add (or the dropped Shortlist idea) is its own row.

### Rerun scope — measured: keep `scope="app"`

**Result (2026-10-12):** a full Deck Editor rerun after an edit (deck caches invalidated, real DB copy, AppTest) takes ~0.21 s of script time, so the Phase 4 convention stays: a board edit ends in `st.rerun(scope="app")` (`deck_board.BOARD_RERUN_SCOPE`). It also avoids a real hazard: after a fragment-only rerun the Mainboard tab's `data_editor` would still hold the old quantities, and its Save would write them back over the edit. Browser-side latency (component re-mount) is not covered by this number.

**Browser-side, Phase C (2026-10-12):** click to re-drawn board, measured in headless Edge with `requestAnimationFrame`, Gisa (100 cards, 27-row Shortlist), real DB copy, 5 runs each: `scope="app"` ~315 ms median for plan / unpair / +1 / −1 alike; `scope="fragment"` ~355 ms, not faster, so `"app"` stays. Of that, ~130 ms is Phase C's all-decks `plan_availability()` (231 open rows), run uncached on every Board render — the one obvious saving if it ever feels slow (cache it in `loaders` and clear it from both `invalidate_deck_caches` and `invalidate_collection_caches`).

Original question:

Phase 4's convention is that writes inside a fragment end in
`st.rerun(scope="app")`, so other parts of the page don't show stale
data. On the Board tab, the only other per-deck content is in other tabs.
**Before building the menus**, time an edit both ways on the real
database: if a full Deck Editor rerun feels instant, keep the convention;
if not, rerun only the fragment and note here that the other tabs refresh
on the next full rerun.

### Mainboard tab

Keep the Mainboard tab's `data_editor` for now — it's still the fastest
way to make many quantity changes at once. Revisit removing it after
Phase C, once the Board has had real use.

### Tests

- `tests/test_deck_board.py` — `apply_board_action()` for every op on the
  conftest DB: quantities, `deck_changes` rows (an `applied` row on
  remove, an `idea` on `to_shortlist`), tags; each returned `undo`
  restores the prior state; invalid payloads raise `ValueError`;
  `dec` from 1 removes the card.
- `tests/test_pages_smoke.py` — Deck Editor renders with the Board tab
  for a normal, empty and new deck; the add bar adds a card. If AppTest
  can set a component trigger value through `session_state`, add one
  end-to-end case; if not, say so here and rely on the handler tests.

### Done when

You can add, bump, remove, retag and shortlist cards from the Board tab,
each edit is one rerun, undo works, and the Mainboard tab and Decks page
reflect the change.

---

## Phase C — Shortlist alongside the deck

Show the Shortlist as a side column on the Board and make it the place to
turn ideas into swaps. This is the old Swap Manager, made visual.

### Data

Add the open Shortlist to the payload as a second list, from
`loaders.load_shortlist_df()` (`change_id`, `status`, `replace_target`,
`notes`, plus card columns). For availability, call
`queries.plan_availability()` over **all decks'** open changes
(`loaders.load_open_changes_df(conn, statuses=("idea", "planned"))` —
its default is planned only — with no deck filter) and keep this deck's
verdicts. The allocator only catches cross-deck collisions when it sees
every deck, which is how the Workbench uses it. Each Shortlist card gets
its verdict's one-line `text` and a color-coded badge (available / in
another deck / need to buy).

### Layout

- A narrow Shortlist column to the right of the stacks (below them on a
  narrow screen), grouped **Ideas** then **Planned**. *As built:* a single
  sticky right rail with the Shortlist above the preview (its own scroll,
  height capped to the window) — a separate column beside the preview left
  the stacks too narrow. On narrow screens the rail goes above the stacks,
  preview first.
- A planned swap draws on both sides: the Shortlist card shows
  "→ replaces X", and the mainboard card X gets a "cut for Y" badge.

### Actions

Click-to-pair first. Drag-and-drop is optional (C2) because it's the most
JS work.

1. Click a Shortlist **idea** → **"Swap in for…"** → the board enters
   pick mode (other cards dim) → click the mainboard card to cut →
   trigger `{"op": "plan", "change_id": N, "cut_id": "<scryfall_id>"}` →
   `writes.promote_change(conn, change_id, remove_scryfall_id=cut_id)`.
   Esc or a second click cancels pick mode, all in the browser.
2. On a **planned** card: **Apply** → `writes.apply_changes(conn,
   [change_id])` (the same path the Deck Editor's Shortlist tab and the
   Workbench use, so history and collection locations update the same
   way); **Unpair** → `writes.demote_change()`; **Drop** →
   `writes.drop_change()`.
3. On an **idea**: **Add to deck** without a cut →
   `writes.add_deck_card()` (its `_auto_drop_stale_ideas` already
   retires the idea).

The `apply_board_action()` handler gains these ops. Write functions raise
`ValueError` with a user-facing reason (wrong state, nothing to replace);
surface it as a toast. Undo for `plan` is `demote_change`. `apply` has no
undo — applying moves collection lots — so it asks for a confirm click in
the component first.

**(C2, optional)** Drag a Shortlist card onto a mainboard card to send the
same `plan` trigger. Only if click-to-pair feels clunky in use.

### Tests

- `tests/test_deck_board.py` — payload includes the Shortlist with
  verdicts; a cross-deck collision (two decks wanting your one copy) shows
  on this deck's card; `plan` / `apply` / `unpair` / `drop` / `add`
  through the handler leave `deck_changes`, `deck_cards` and `collection`
  in the same state as the Shortlist tab's existing path
  (compare against `tests/test_deck_changes_writes.py`'s expectations).
- Smoke: Board tab renders with an empty Shortlist, ideas only, and a
  planned swap.

### Done when

You can take an idea from the Shortlist column, pair it with a card to
cut, see its availability, and apply it, all from the Board, with the
Workbench showing the same result.

---

## Out of scope for this plan

Kept for later so this plan stays to three phases: sample hand /
goldfish, global card search, hover previews in other tables, deck diff /
compare, a phone-friendly game log, and the double-faced card back face
in the preview (the cache stores the front face only).

## Conventions to hold to

Carried forward from [UI_ACTION_PLAN.md](docs/archive/UI_ACTION_PLAN.md):

- **`queries.py` reads, `writes.py` writes, `loaders.py` caches, pages
  render.** `deck_board.py` is UI glue: it may call writes, but holds no
  SQL. This plan needs no schema change; if one seems needed, stop and
  flag it (additive only, via `_SCHEMA_UPGRADES`).
- **Module header comments + inline comments for non-obvious logic**, in
  the JS too. Update the Decks and Deck Editor header docstrings when
  their layout changes.
- **Tests in `tests/`** against the in-memory `conftest.py` fixture; every
  touched page is smoke-tested with `AppTest` before it's called done.
- **Run with the `mtg1` conda env.**
- Update `PROJECT_STATE.md` and `README.md` at the end of each phase, and
  tick the checklist above with a one-line "done" note.

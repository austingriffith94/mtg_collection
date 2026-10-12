# UI Action Plan — Navigation, Layout, Speed, and Polish

Working plan for the UI review of 2026-10-12. The data layer and features
are in good shape; this pass is about how the app *feels* to use: finding
pages, keeping context between them, long pages, rerun lag, and visual
consistency. Previous plans are finished and archived in
[docs/archive/](docs/archive/README.md).

Each phase can ship on its own: the app runs, tests pass, and no page is
half-converted at a phase boundary. Do them in order. Phase 0 builds the
smoke-test harness every later phase uses; Phase 1 changes how pages are
registered, and Phase 2's `st.page_link` cross-links need that (links only
work to pages the router knows about). Phase 3 moves content onto the
Phase 1 Home page, and Phase 5's `st.pills` and dialogs assume the
Phase 0 version pin.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done

- [x] **Phase 0** — Foundations: version pin, `width=` migration, theme file, stale docs. Done: `requirements.txt` pinned to `streamlit>=1.63`; all 31 `use_container_width=True` call sites → `width="stretch"`; `.streamlit/config.toml` theme block added; `tests/test_pages_smoke.py` runs all 10 pages via `AppTest` (also fixed a latent crash in `2_Decks.py`'s printable-sheet price `nlargest` on an all-null price column); `PROJECT_STATE.md`/`dashboard.py` stale-doc spots corrected.
- [x] **Phase 1** — Grouped navigation + new Home page. Done: `dashboard.py` is now a router over `dashboard_lib/nav.py`'s `PAGES` registry (4 sections, uppercase titles, explicit `url_path`s, `Decks` kept); old Home content moved to `pages/0_Home.py` with a registry-built `st.page_link` card grid replacing the text guide; `inject_nav_caps_css()` deleted; DB caption moved to a sidebar footer. Checked on 1.63: per-page `set_page_config` still works under `st.navigation` (its `page_title` sets the browser tab), and `AppTest` drives the router incl. `switch_page`. Home is router-only (its `page_link` paths are relative to `dashboard.py`), so the smoke test now runs every page through the router plus every non-Home page standalone; `tests/test_nav.py` covers the registry. Compare-all-decks slot left for Phase 3.
- [x] **Phase 2** — One shared "current deck" across pages + cross-links. Done: `card_view.render_deck_picker()` now selects by deck *id* and owns the shared `current_deck_id` session key (copied into each page's own widget key before drawing, written back in `on_change`); pure helpers `seed_current_deck()` (a `?deck_id=` param seeds once per value per session, then the session key wins) and `resolve_current_deck()` (falls back to the first deck). Decks, Land Probability, Deck Editor and the Advisor's Analysis tab all share it; the Editor's create flow selects the new deck by id. `nav.render_deck_links()` adds `st.page_link` cross-links (← Decks / ✏️ Edit deck / 🎲 Probabilities / 🧠 Ask the Advisor, minus the current page) and skips itself when a page runs standalone (no router → `StreamlitPageNotFoundError`). Home tiles are full-reload `<a>` links, so a tile click starts a fresh session and the param always seeds. Tests: `tests/test_current_deck.py` (pure) plus AppTest cases in `test_pages_smoke.py` (each page opens on the current deck, the deck carries across pages through the router, param-then-manual-pick, create-deck selects the new deck).
- [x] **Phase 3** — Split the Decks page into tabs. Done: `pages/2_Decks.py` keeps the branded header, cross-links and Turn 0 stat grid above five tabs (Overview, Mana & Curve, Combos, Cards, History), each a `_render_<tab>()` function in the same file; Moxfield export + printable sheet merged into one "📤 Export" expander (sub-headings, since Streamlit can't nest expanders — still built eagerly until Phase 4); the Cards browser's filters moved from the sidebar into a "🔍 Filters" expander on that tab; "📊 Compare all decks" moved to Home under the tiles. Tests in `test_pages_smoke.py`: tab labels for each deck, brewing + empty deck, Compare on Home and not on Decks.
- [ ] **Phase 4** — Speed: static images, fragments, lazy exports
- [ ] **Phase 5** — Better controls: segmented/pills, dialogs, safer edit table
- [ ] **Phase 6** — Copy cleanup + consistent chart colors
- [ ] **Phase 7** — Launcher (`run.py`) tidy-up

---

## Phase 0 — Foundations

Small, low-risk groundwork that later phases rely on.

- **Version pin.** `requirements.txt` says `streamlit>=1.32`, but the
  `mtg1` env runs 1.63, and this plan uses `st.navigation` sections,
  `st.dialog`, `st.fragment`, `st.segmented_control`/`st.pills` and
  `width="stretch"`. Pin to the version actually tested here
  (`streamlit>=1.63`) rather than guessing the oldest release that has all
  of them.
- **`use_container_width` → `width="stretch"`.** About 30 call sites across
  `pages/` and `dashboard_lib/` (`grep -rn use_container_width`). Mechanical
  change; removes deprecation warnings.
- **Config file.** Add `.streamlit/config.toml` (the folder has only the
  gitignored `secrets.toml` today) with a `[theme]` block: base, primary
  color, background/secondary background, font, and corner radius. Check
  that the file is *not* caught by `.gitignore` (only `secrets.toml` is).
  Phase 4 adds a `[server]` section to this same file.
- **Page smoke-test harness.** `tests/` has no `AppTest` coverage today
  (`conftest.py` only builds an in-memory connection, and pages call
  `db.get_connection()` against the real `DB_PATH`). Every later phase
  says "AppTest the page", so build the harness first:
  `tests/test_pages_smoke.py` plus a fixture that builds a small temp
  database (reuse the `conftest.py` schema/seed helpers), points
  `db.DB_PATH` at it, and runs each file in `pages/` through
  `AppTest.from_file(...)`, asserting no exception. Never run it against
  the live `mtg_collection.db`.
- **Stale docs.** In `PROJECT_STATE.md`: "7 pages" (line 8), the "The 7
  pages, in brief" heading (line 102), and the "no live Streamlit or
  network access" caveat (line 230). There are 10 pages and `AppTest`
  runs fine here. Also the `dashboard.py` docstring still says the
  launcher option is "2) Launch dashboard" (it's 1).

**Done when:** `grep use_container_width` returns nothing, the smoke test
runs all 10 pages with no exceptions, and the theme shows.

## Phase 1 — Grouped navigation + new Home page

**Problem.** Ten pages in one flat sidebar list. The ALL-CAPS labels come
from CSS aimed at Streamlit's internal `data-testid` hooks
([card_view.py `inject_nav_caps_css`](dashboard_lib/card_view.py)), which
its own comment flags as likely to break on upgrade. The Home page's
page guide ([dashboard.py](dashboard.py)) is a long text block that is
already out of date: no Workbench, Card Lookup or Advisor, and it still
calls the two editors "Editor".

**Changes.**

- `dashboard.py` becomes a small router: `st.navigation({...})` with
  sections, then `pg.run()`. Once `st.navigation` is used, Streamlit stops
  auto-discovering `pages/`, so the files can stay where they are.

  | Section | Pages |
  |---|---|
  | Browse | Home, Collection, Decks, Card Lookup |
  | Plan | Workbench, Advisor, Land & Color Probability |
  | Edit | Deck Editor, Collection Editor, Card Database |
  | Play | Commander Game Tracking |

- Move the current Home content into `pages/0_Home.py` (`default=True`).
- Give every `st.Page` an explicit `url_path`. **The Decks page must stay
  `Decks`**: home tiles link to `Decks?deck_id=N`
  (`render_deck_landing_grid(deck_page_path="Decks")`).
- Keep the ALL-CAPS look (a Prompt Pass 8 request) by passing uppercase
  `title=` values, then delete `inject_nav_caps_css()` and drop it from
  `setup_page()`.
- Replace the Home page's text guide with a grid of `st.page_link` cards
  (icon + name + one-line purpose), built from the same page registry as
  the router so the two can't drift apart. Keep the metrics row, the
  Workbench summary line and the deck tiles, and leave room for the
  "📊 Compare all decks" expander that Phase 3 moves here from the Decks
  page.
- Move the "Reading from `mtg_collection.db`" caption into a sidebar footer
  (or drop it).

**Watch out for.**
- `st.set_page_config` is called by every page via `setup_page()`. Check on
  1.63, with one converted page, that calling it from a page script under
  `st.navigation` still works (and what it does to the browser-tab title
  next to the uppercase `st.Page` title) before converting all ten.
- Pages are no longer entry scripts, so the per-page
  `sys.path.insert(...)` lines become redundant (the router's own insert
  covers them). Leave them alone for now so a page can still be run on its
  own; don't remove them as part of this phase.
- Confirm `AppTest.from_file("dashboard.py")` can drive a navigation
  router on 1.63. If it can't, test each page file directly (the Phase 0
  harness) and test the registry separately.
- Docs that list the page set need updating: `README.md`'s page sections
  and `PROJECT_STATE.md`.

**Tests.** The Phase 0 smoke test over every page; a test that every
registry entry's file exists and no file in `pages/` is missing from the
registry; a test that the Decks `url_path` is `Decks`.

## Phase 2 — One shared "current deck"

**Problem.** Decks, Land Probability and Deck Editor each keep their own
picker key (`deckpage_deck_select`, `landprob_deck_select`,
`editor_deck_select`). Choosing a deck on one page and opening another
means choosing it again, and there are no links between a deck's views.

**Changes.**

- One plain session key, `current_deck_id`, owned by
  `cv.render_deck_picker()`. Streamlit **deletes a widget's state when that
  widget isn't drawn in a run**, so the picker's widget key can't hold the
  value across pages. Instead: copy `current_deck_id` into the widget key
  just before drawing the picker, and write the choice back in its
  `on_change`.
- **Store the deck *id*, not its name.** Today the picker's value is the
  deck's name (`deck_labels`), and the Deck Editor's "create deck" flow
  sets `st.session_state["editor_deck_select"] = cn_name.strip()`
  ([4_Deck_Editor.py:79](pages/4_Deck_Editor.py#L79)). Both break under a
  shared key (and a rename would orphan the saved selection). Change that
  create path to set `current_deck_id` to the new deck's id, and make the
  rename path keep it (the id doesn't change).
- **Deep links vs. the URL (conflict with today's code).** The Decks page
  currently *deletes* `?deck_id=` right after reading it, specifically so a
  leftover param doesn't override a later manual pick. So don't simply
  "also write the param on change". Rule: a `deck_id` param seeds
  `current_deck_id` only when it differs from the last param already
  consumed this session (track it in `session_state`); after that the
  session key wins. Keep deleting the param after reading, as now. If
  bookmarkable URLs are wanted, that's a separate, optional step that must
  honor the same rule. Note that Streamlit also clears query params when
  you switch pages, so the session key (not the URL) is what carries the
  deck between pages.
- Cross-links under the deck header (`st.page_link`): **✏️ Edit deck**,
  **🎲 Probabilities**, **🧠 Ask the Advisor**, and **← Decks** from the
  Editor/Probability pages. The Advisor's deck selectbox
  ([10_Advisor.py](pages/10_Advisor.py)) joins the shared key as well. The
  Advisor selects by `deck_id` already, so it needs the key only, not the
  name mapping.

**Tests.** Pure test of the seed/sync helper, including the "param already
consumed, then manual pick wins" case and the create-deck path; AppTest:
set `current_deck_id`, render each deck page, confirm they all show that
deck.

## Phase 3 — Split the Decks page into tabs

**Problem.** [pages/2_Decks.py](pages/2_Decks.py) is about 1,000 lines on
one scrolling page: header, stat grid, exports, build plan, win cons,
mana/Game Changers/Reserved List, combos, themes/curve/types/tags, top-10
prices, change history and the card browser, with 9 dividers between them.

**Changes.** Keep the branded header and the "Turn 0" stat grid above the
tabs. Everything else goes into tabs:

| Tab | Contents |
|---|---|
| Overview | Win cons / strengths / weaknesses, optimized mana, Game Changers, Reserved List, build plan (if brewing) |
| Mana & Curve | Themes, mana curve + color breakdown, type breakdown, strategy tags |
| Combos | Commander Spellbook panel (in deck / one card away) |
| Cards | Mainboard/Shortlist browser, top-10 most expensive |
| History | Change log |

- **Exports** (Moxfield, printable sheet) are actions on the whole deck,
  not history. Put them in a single "Export" expander in the header area
  above the tabs (built lazily, see Phase 4).
- **Sidebar filters.** The Cards browser's filters are in the sidebar
  ([2_Decks.py:955](pages/2_Decks.py#L955), `container=st.sidebar`), so
  after the split they'd show on every tab while only affecting Cards. Move
  them into the Cards tab (a `st.popover` or an expander above the
  browser). `render_filter_panel` already takes a `container=`, so this is
  mostly passing a different container. Keep the sidebar for the deck
  picker only.
- Move "📊 Compare all decks" off this page and onto the **Home page**
  (as a collapsed expander under the deck tiles; add it to Phase 1's Home
  build list). It's a cross-deck view and sits above a single deck here.
- Split the page into a few `_render_<tab>(conn, deck_id)` functions in the
  same file (or `dashboard_lib/deck_page.py` if the file is still too long).
  Behavior stays the same; only the layout changes.
- Note that `st.tabs` runs *every* tab's code on every rerun. Phase 4's
  fragments deal with the cost; don't put network calls in a tab body (the
  Spellbook button already avoids this).

**Tests.** AppTest the page for a normal deck, a brewing deck and a deck
with no cards; check each tab label is present and nothing raises.

## Phase 4 — Speed

**Problem.** Every widget change reruns the whole page, and the pages
re-send a lot of data each time.

- **Images as base64.** `card_view` reads each cached image and puts it into
  HTML as a `data:` URI (around [card_view.py:405-523](dashboard_lib/card_view.py)).
  A 48-card grid sends several MB on every rerun. Fix: add
  `[server] enableStaticServing = true` to `.streamlit/config.toml` and
  serve images as `/app/static/<path>`. Static files must live in
  `static/` next to the entry script (`dashboard.py`).
  - **Paths are checked:** `cards.local_image_path` and deck cover paths
    are stored *relative* to the project root (`image_cache/<id>.jpg`,
    `image_cache/deck_covers/12.png`), so **no database rewrite is
    needed**. Move the folder to `static/image_cache/` and resolve stored
    paths against `static/` instead of `BASE_DIR`
    ([formatting.resolve_local_image](dashboard_lib/formatting.py)); the
    stored values stay valid. The browser URL is then
    `/app/static/` + the stored path.
  - Places that must change together: `db.IMAGE_CACHE_DIR`,
    `DECK_COVER_DIR` in [4_Deck_Editor.py:42](pages/4_Deck_Editor.py#L42),
    the cover-saving code in `writes.py` (~line 366),
    `scripts/sync_images.py` (its own `IMAGE_CACHE_DIR` + `rel_path`),
    `.gitignore` (`/image_cache/` → `/static/image_cache/`), and the
    offline-test fixtures that use `image_cache/...` strings.
  - `deck_printout` and any download/export that needs a self-contained
    file keeps embedding base64; only the on-screen grids and tiles switch
    to URLs. Keep the remote Scryfall URL fallback.
  - Moving the user's existing cache is a one-time file move; do it in the
    phase's change as a small, idempotent step in `run.py`/a script, not by
    hand.
- **Fragments.** Wrap self-contained interactive panels in `@st.fragment`:
  the browser (view/sort/page controls + grid), the Combos tab, the
  Collection edit table, and the Workbench's per-deck plan blocks. Rules
  that make this safe:
  - A fragment reruns with the arguments it had on the last *full* run, so
    pass it the already-filtered data frame from outside and let the
    sidebar filters trigger the full rerun (they live outside it).
  - Don't draw into the sidebar or other outside containers from inside a
    fragment; keep every fragment's widgets in its own container.
  - Any write that changes things outside the fragment (a tag edit that
    changes the stat grid, a lot edit that changes the metrics row) ends
    with `st.rerun(scope="app")`.
- **Lazy exports.** A collapsed `st.expander` still runs its code. The
  Collection page's Moxfield expander
  ([1_Collection.py](pages/1_Collection.py)) builds the full CSV on every
  run, and the Decks page's export and printable sheet do the same. Build
  them only when a "Prepare export" button is clicked, or inside an
  `st.dialog`.

**Done when:** Collection in grid view (48/page) no longer embeds image
bytes in the page, so a filter change is light and paging or sorting
reruns only the browser fragment. Record page payload size before and
after (browser devtools, Network tab) in the "as built" note. Note that a
filter change is still a full rerun by design; the win there is payload
size, not skipping the rerun.

## Phase 5 — Better controls

- **Segmented controls / pills** for small fixed choices:
  Table / Image Grid (`render_browser`), Deck status (Collection),
  Full / Not-in-deck (export scope), Mainboard / Shortlist (Decks), the
  curve breakdown radio, and the Yes/No/All bool filters in
  `render_filter_panel`. Color identity becomes WUBRG+C pills
  (`st.pills`, multi-select) instead of a multiselect.
  - **Unlike a selectbox/radio, these can end up with nothing selected**
    (they return `None`). Treat `None` as the default ("All" / no filter /
    Table view) everywhere they're read, and keep `register_filter_key()`
    for each so "Refresh data" still resets them.
  - The Collection page's "Colorless" option and the Decks page's
    `multi_facets` use the same filter panel, so change it once in
    `render_filter_panel` and check both pages.
- **Dialogs (`st.dialog`)** for create/edit/delete flows that now sit in
  inline expanders:
  - Game Tracking: edit game, and delete game. **Delete has no
    confirmation today**, so the dialog adds a confirm step.
  - Deck Editor: "Create a new deck" (now a sidebar expander).
  - Master-list management (themes, locations, GC categories, mana tags)
    on Deck Editor, Collection Editor and Card Database.
  - Only one dialog can be open at a time, and dialogs can't be nested.
    Keep each one to a single job.
- **Collection edit table.** Its caption warns that changing a filter
  discards unsaved edits. Turn the sidebar filters off (`disabled=True`)
  while Edit mode is on, and show a "N unsaved changes" count from the
  editor's `edited_rows` next to the Save button. Two details: the filters
  are drawn *before* the Edit-mode toggle in script order, so read the
  toggle from `st.session_state["collection_edit_mode"]` and add a
  `disabled` parameter to `render_filter_panel`; and the count must be
  computed inside the same fragment as the editor (Phase 4).
- **Feedback.** Use `st.toast` for successful saves app-wide (Collection
  Editor already does), and keep `st.success`/`st.error` for things the
  user needs to read.

**Tests.** AppTest for the game-delete dialog (cancel leaves the game,
confirm removes it); writes-level tests already cover the deletes
themselves.

## Phase 6 — Copy cleanup + chart colors

- **Captions.** Many page tops repeat explanations, for example "Writes
  straight to the database — no CSV editing or re-migration needed" on
  three pages. Rule: explanations go in `help=` tooltips or a single
  collapsed "ℹ️ How this works" expander per page; `st.caption` is kept
  for short context and warnings. Go through it page by page.
- **One color map.** Add `formatting.MANA_COLORS` (W/U/B/R/G/C/multicolor)
  and use it in every chart that splits by color: the curve/color
  breakdown, color-source probability, weighted mana graph, and
  win-rate-by-deck colors where relevant. Pick a second, separate
  palette for non-color categories so a "Red" bar always means red mana.
- **Empty states.** Make the "No X yet" captions follow one pattern
  (what's missing + where to add it, linking with `st.page_link`).

## Phase 7 — Launcher tidy-up

- [run.py](run.py) repeats the same `if not db_exists` block five times.
  Replace it with a menu table (`key, label, func, needs_db`) and one
  dispatch loop.
- When there's no DB, show only `migrate` and Exit instead of tagging every
  option "[needs the initial migration first]".
- Add `python run.py --dashboard` (and make `run.bat`/`run.command`/
  `run.sh` accept it) to launch straight into the app, skipping the menu.
- Keep `migrate` behind typing the word + `REBUILD` confirmation as now.

---

## Conventions to hold to

Carried forward from the archived plans:

- **`queries.py` reads, `writes.py` writes, `loaders.py` caches, pages
  render.** This is a UI pass. If a phase seems to need a schema change,
  stop and flag it; any schema change stays additive via
  `_SCHEMA_UPGRADES`.
- **Module header comments + inline comments for non-obvious logic.**
  Update each page's header docstring when its layout changes. Many
  describe the layout in detail and will go stale.
- **Tests in `tests/`** against the in-memory `conftest.py` fixture; every
  touched page is smoke-tested with `AppTest` before it's called done.
- **Run with the `mtg1` conda env** (base Python lacks the deps).
- Update `PROJECT_STATE.md` and `README.md` at the end of each phase, and
  tick the checklist above with a one-line "done" note, as the archived
  plans did.

## Backlog (carried over)

Open items from the archived plans. They're not part of this UI pass, but
they're kept here so they don't get lost:

- **Proxies.** Still no real `proxy` concept. Contention (buy vs. take)
  and a brew's sourcing plan are where "proxy it" belongs as a third
  option. Needs a decision before any work. *(from IMPLEMENTATION_PLAN)*
- **`location_catalog` naming.** Bare entries `3` and `4` sit next to
  `Box` / `Lands Box` / `Lands`. If they're box numbers, rename them to
  `Box 3` / `Box 4` and backfill the 8 existing lots on purpose.
  *(from IMPLEMENTATION_PLAN)*
- **Advisor → Shortlist.** Write suggested changes into `deck_changes` as
  `idea` rows after explicit confirmation. Fits naturally after Phase 2's
  cross-links. *(from ADVISOR_PLAN "Later")*
- **Advisor preferences.** A notes table for user preferences the advisor
  can read. *(from ADVISOR_PLAN "Later")*
- **Ollama / local model.** Skipped by user decision; Ollama isn't
  installed and ROCm GPU support on the RX 6800 XT was never verified.
  Revisit only if a local model becomes worth it.
  *(ADVISOR_PLAN Phase 2)*

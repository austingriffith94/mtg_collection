# UI Action Plan — Navigation, Layout, Speed, and Polish

Working plan for the UI review of 2026-10-12. The data layer and features
are in good shape; this pass is about how the app *feels* to use: finding
pages, keeping context between them, long pages, rerun lag, and visual
consistency. Previous plans are finished and archived in
[docs/archive/](docs/archive/README.md).

Each phase can ship on its own: the app runs, tests pass, and no page is
half-converted at a phase boundary. Do them in order. Phase 1 changes how
pages are registered, and Phases 2-3 build on that.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done

- [ ] **Phase 0** — Foundations: version pin, `width=` migration, theme file, stale docs
- [ ] **Phase 1** — Grouped navigation (`st.navigation`) + new Home page
- [ ] **Phase 2** — One shared "current deck" across pages + cross-links
- [ ] **Phase 3** — Split the Decks page into tabs
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
  `width="stretch"`. Raise the pin to `streamlit>=1.50`.
- **`use_container_width` → `width="stretch"`.** About 30 call sites across
  `pages/` and `dashboard_lib/` (`grep -rn use_container_width`). Mechanical
  change; removes deprecation warnings.
- **Theme.** Add `.streamlit/config.toml` (the folder has only the
  gitignored `secrets.toml` today) with a `[theme]` block: base, primary
  color, background/secondary background, font, and corner radius. Check
  that the file is *not* caught by `.gitignore` (only `secrets.toml` is).
- **Stale docs.** `PROJECT_STATE.md` still says "7 pages" and carries the
  "no real Streamlit in the sandbox" caveat; there are 10 pages and
  `AppTest` runs fine here. Fix both lines.

**Done when:** `grep use_container_width` returns nothing, every page
renders under `AppTest` without deprecation warnings, and the theme shows.

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
  Workbench summary line and the deck tiles.
- Move the "Reading from `mtg_collection.db`" caption into a sidebar footer
  (or drop it).

**Watch out for.** `st.set_page_config` is called by every page via
`setup_page()`; with a router, call it once there and make the per-page
call set only title/icon. Check this on 1.63 before converting all pages.

**Tests.** AppTest the router and each page; a test that every registry
entry's file exists; a test that the Decks `url_path` is `Decks`.

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
- Fold the Decks page's `?deck_id=` handling into the picker, so any deck
  page reached by URL picks it up. Also set `st.query_params["deck_id"]` on
  change so the URL can be bookmarked or shared.
- Cross-links under the deck header (`st.page_link`): **✏️ Edit deck**,
  **🎲 Probabilities**, **🧠 Ask the Advisor**, and **← Decks** from the
  Editor/Probability pages. The Advisor's deck selectbox
  ([10_Advisor.py](pages/10_Advisor.py)) joins the shared key as well.

**Tests.** Pure test of the seed/sync helper; AppTest: set
`current_deck_id`, render each deck page, confirm they all show that deck.

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
| History | Change log; Moxfield export and printable sheet |

- Move "📊 Compare all decks" off this page and onto the Home page (or into
  Workbench). It's a cross-deck view and sits above a single deck here.
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
  A 48-card grid sends several MB on every rerun. Fix: turn on
  `server.enableStaticServing` and serve images as
  `/app/static/<file>`. Static files must live in `static/` next to the
  entry script, so either move `image_cache/` to `static/image_cache/`
  (update `db.IMAGE_CACHE_DIR`, `scripts/sync_images.py`, `.gitignore`)
  or use a directory junction. **Check first** whether
  `cards.local_image_path` stores absolute or relative paths. If absolute,
  this needs a one-off path rewrite (additive, through `writes.py`).
  Keep the remote Scryfall URL fallback.
- **Fragments.** Wrap self-contained interactive panels in `@st.fragment`:
  the browser (view/sort/page controls + grid), the Combos tab, the
  Collection edit table, and the Workbench's per-deck plan blocks. Any
  write that changes things outside the fragment ends with
  `st.rerun(scope="app")`.
- **Lazy exports.** A collapsed `st.expander` still runs its code. The
  Collection page's Moxfield expander
  ([1_Collection.py](pages/1_Collection.py)) builds the full CSV on every
  run, and the Decks page's export and printable sheet do the same. Build
  them only when a "Prepare export" button is clicked, or inside an
  `st.dialog`.

**Done when:** a sidebar filter change on Collection (grid view, 48/page)
feels instant; compare page payload size before and after in browser
devtools and record both numbers in the "as built" note.

## Phase 5 — Better controls

- **Segmented controls / pills** for small fixed choices:
  Table / Image Grid (`render_browser`), Deck status (Collection),
  Full / Not-in-deck (export scope), Mainboard / Shortlist (Decks), the
  curve breakdown radio, and the Yes/No/All bool filters in
  `render_filter_panel`. Color identity becomes WUBRG+C pills
  (`st.pills`, multi-select) instead of a multiselect.
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
  editor's `edited_rows` next to the Save button.
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

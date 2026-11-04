# Goldfish Plan — play out a deck's first turns by clicking

Working plan from the discussion of 2026-11-03, revised the same day after
checking it against the code and the live card data. Goal: pick a deck, draw
an opening hand, and play out the first few turns by clicking cards to play
them and clicking permanents to tap them. Nothing is dragged. The page helps
you judge a deck's early game (land count, ramp, curve, and the turn you cast
your commander) by playing hands instead of reading odds.

Each phase ships on its own: the app runs, tests pass, and nothing is
half-converted at a phase boundary. Go in order. Work happens on the
`goldfish` branch, one commit per phase.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done

- [x] **Phase 0** — Branch and plan.
- [x] **Phase 1** — Deck payload and mana parsing (pure Python, tested).
- [x] **Phase 2** — The Goldfish page and component: zones, turns, mulligan, tap, log, undo.
- [x] **Phase 3** — Hints and library actions: castable marks, search, scry, extra draw.
- [ ] **Phase 4** — Turn summary, docs and wrap-up.

## Decisions

Agreed 2026-11-03:

- **Click, don't drag.** You click a hand card to play it and click a
  battlefield card to tap or untap it. Rarer moves are in a small ⋯ menu on
  the card.
- **A game is never saved.** It is gone when you leave the page, change decks
  or close the tab. There is no seed, no replay, no saved notes and no
  database table. This is on purpose, to keep it simple.
- **The game runs in the browser.** It is a `st.components.v2` component,
  built like the deck board (`dashboard_lib/components/`, JS and CSS read at
  import time, no npm or build step). Shuffling, drawing, tapping and turn
  changes never cause a Streamlit rerun. A rerun per click would reload every
  card image and feel slow.
- **Python sends the deck once and never hears back.** `goldfish_payload()` is
  pure and unit-tested. The component has no callbacks, so Python takes no
  part in play and no game state goes into `st.session_state`.
- **The game survives reruns by living outside the component function.**
  The component function runs again on every data update, with the same
  `parentElement`, and the deck board's own code tears down the previous run
  each time (`deck_board.js`). So the game lives in a store on `parentElement`
  (not in the function's closure), and each run only redraws from it. The
  component is keyed per deck (`goldfish_<deck_id>`), so changing deck mounts a
  fresh component with a fresh game. That is the "hard reset" above, with no
  extra code.
- **Read-only.** The page never writes to the database.
- **Its own page**: `pages/11_Goldfish.py`, in the **Play** section of
  `nav.PAGES`. It is also added to `nav.DECK_LINKS`, so every per-deck page
  (Decks, Deck Editor, Probabilities, Advisor) links to it. It follows
  `card_view.CURRENT_DECK_KEY` like the others.
- **Mana is manual, with hints.** You tap your own sources. The page shows
  the mana your untapped sources can make and marks the cards you could cast
  with it, but it never pays for a spell for you. Automatic payment gets the
  edge cases wrong (filter lands, hybrid costs, X spells) and then the hints
  can't be trusted.
- **When the hint can't know, it says `?`.** A source whose output can't be
  read from its text (Cabal Coffers, "for each Swamp", filter lands) counts as
  unknown. The mana readout shows it as `4 + ?`, and a spell whose cost can't
  be read gets no ✦.
- **Rules are shown, not enforced.** A second land drop in a turn shows a
  warning but is allowed. Summoning sickness, enters-tapped lands and
  commander tax are tracked so the hints stay honest, but nothing is blocked.
  Goldfishing is practice, and some cards break the normal rules (extra land
  drops, haste).
- **No life, combat or stack.** You're playing alone, so there's nothing to
  attack.
- **Mainboard only.** Commander(s) start in the command zone and the rest of
  `deck_cards` makes up the library. The maybeboard and shortlist are left out.
  A deck that isn't 100 cards (a brewing deck) can still be played, with a
  caption giving its card count.
- **Cards with two faces ask when you click them.** There is no choice to make
  beforehand. Clicking such a card in your hand opens a small chooser at the
  card showing each face by name and type ("Play Boggart Bog — land" /
  "Cast Boggart Trawler — creature"). Pick one and the card goes where that
  face belongs. Escape or a click elsewhere cancels and leaves the card in your
  hand. The card on the battlefield then shows as the face you chose. A card
  with a spell on both faces (Consecrate // Consume, an adventure) uses the
  same chooser; with a land on both faces (a Pathway) the chooser picks which
  land. Single-face cards skip the chooser.

## Data findings behind the design

Checked against `mtg_collection.db` (read-only) on 2026-11-03. These are why
parts of the design are shaped the way they are:

- **Multi-face cards store no mana cost.** Boggart Trawler // Boggart Bog,
  Hagra Mauling // Hagra Broodpit and 10 non-land cards such as Shaile, Dean of
  Radiance // Embrose have `mana_cost = NULL`. Their `type_line` and
  `oracle_text` are both faces joined by ` // `. So every text rule is applied
  **per face**, never to the joined text, or the back land's "Add {B}" would
  be read as the front spell's.
- **Dual lands say `Add {G} or {W}`.** Counting symbols would give 2 mana for a
  land that makes 1. Triomes would give 3.
- **Some cards have several mana abilities.** Talisman of Dominance has
  `Add {C}` and `Add {U} or {B}` as separate abilities.
- **Command Tower's text is "any color in your commander's color identity."**
  It is treated as any color, which is right for a goldfish since the deck is
  built inside that identity.

## The design

### Payload (`dashboard_lib/goldfish.py`)

`goldfish_payload(cards_df, deck_meta, layer_tags=None)` takes the same
inputs as `deck_board.board_payload` (`loaders.load_deck_cards_df`,
`loaders.load_deck_meta`) and returns
`{"commanders": [...], "library": [...], "warnings": [...]}`.

**Cards are sent once with a `qty`** and the component expands them into
copies with ids `"<scryfall_id>#1"` … `"#30"`, because each copy can be tapped
separately. This keeps 30 Forests to one entry in the payload.

Each card dict holds, per face (a one-face card has one face):

| Field | Source | Used for |
|---|---|---|
| `id`, `name`, `img`, `qty` | row, `card_view.card_image_src` | display, hover preview |
| `cmc` | row | mana value, summary |
| `faces[]` | `type_line` and `oracle_text` split on ` // ` | one entry per face, below |
| `is_commander` | `deck_meta` commander / partner | command zone |
| `tags` | `strategy` layer (+ `layers` like the board) | small role badges (Ramp, Draw…) |

Each face holds:

| Field | Meaning |
|---|---|
| `name`, `type_line`, `kind` | `kind` is `land`, `creature`, `permanent` (artifact, enchantment, planeswalker, battle) or `spell` (instant, sorcery) |
| `cost` | `{"generic": n, "W": n, …, "C": n, "hybrid": [["W","U"]], "twobrid": ["W"], "phyrexian": ["B"], "snow": n, "x": bool}`, or `null` when the cost can't be read |
| `mana` | what the face taps for, see below, or `null` for no mana ability |
| `enters_tapped` | `"always"`, `"maybe"` or `null` |

The component never parses a `{2}{G}` string. The cost is parsed once in
Python, where it is unit-tested. The table stores one `mana_cost` per card, so
a multi-face card with `mana_cost = NULL` has no readable cost on any face:
every face's `cost` is `null` and the card gets no ✦, as agreed. Face costs
are not worth fetching for this.

**`mana`** is a list of abilities, each `{"colors": [...], "any": bool,
"amount": n | null}`. An ability reads from the face's own text:

- Split the ability on its `or` alternatives (`Add {G} or {W}` is two
  alternatives of 1 mana; `Add {C}{C}` is one alternative of 2). The amount is
  the largest run of adjacent symbols in any alternative.
- `any` comes from `fmt.produces_any_color` (Birds, Command Tower, City of
  Brass).
- Anything that can't be read this way gets `amount: null`: "for each …",
  "X mana", a payment before the "Add" (filter lands, Cabal Coffers), "Spend
  this mana only to …" is read as normal mana. A null amount is the `?` in the
  readout.
- Only lands, and nonland artifacts and creatures, get `mana`
  (`fmt.has_land_face` / `fmt.is_mana_rock_or_dork`, the same gate the Land
  Probability page uses).

A source with several abilities (Talisman) is one source: it can tap for any one
of them, so the matching below treats it as one source with several options.

**`enters_tapped`** uses a regex on the land face's own oracle text. "enters
tapped" or "enters the battlefield tapped" with no condition gives `"always"`.
Any conditional version (unless / if you control …, "you may pay 2 life. If you
don't…") gives `"maybe"`: the card enters untapped and carries a badge, and you
click to tap it if the condition applies.

**`warnings`** holds things the page should say instead of failing quietly:
no commander matched in the deck (see Edge cases), and a deck with fewer than
7 cards in the library.

### The component (`dashboard_lib/components/goldfish.js` / `.css`)

The file has two parts. The **rules part** has no DOM code: state, shuffle,
draw, mulligan, play, tap, castable check, search and scry. The **drawing
part** reads state and renders it. Keeping them apart is what lets the checks
call the rules directly (see Phase 2 and 3).

Layout, top to bottom:

```
┌ Turn 3 · On the play · Land drop: ✓ used ─────── [Next turn ▶] [Undo] [Reset] ┐
│ Mana available: 4 + ?  (G G U C)     Library 89 · Graveyard 1 · Exile 0       │
├ COMMAND ─────┬ LANDS ─────────────────────────────────────────────────────────┤
│ [Commander]  │ [Forest] [Forest]⤵ [Island]⤵                  ⤵ = tapped/dimmed │
│ cast · tax 0 ├ PERMANENTS ────────────────────────────────────────────────────┤
│              │ [Sol Ring]⤵ [Llanowar Elves]                                    │
├──────────────┴────────────────────────────────────────────────────────────────┤
│ HAND  [Cultivate✦] [Counterspell✦] [Big Dragon] [Plains]   ✦ = castable now   │
├───────────────────────────────────────────────────────────────────────────────┤
│ LOG  T1: Forest, Llanowar Elves · T2: Forest, Sol Ring · T3: Island, …        │
└───────────────────────────────────────────────────────────────────────────────┘
```

**Clicks:**

| Click | Does |
|---|---|
| Hand card, single face, land | to Lands (tapped if `enters_tapped == "always"`); uses the land drop |
| Hand card, single face, permanent | to Permanents (creatures marked summoning sick this turn) |
| Hand card, single face, instant / sorcery | to Graveyard, logged as cast |
| Hand card, two faces | chooser at the card (see Decisions), then one of the rows above for the chosen face |
| Battlefield card | tap / untap |
| Commander (command zone) | cast to Permanents; counts one cast |
| ⋯ on any card | to hand · graveyard · exile · top / bottom of library · (commander) back to the command zone |
| Graveyard / Exile count | opens the list; ⋯ works there too |
| Hover | full card preview, as on the deck board |

**Commander tax** is `2 × (casts − 1)`, from a per-commander cast counter.
Returning a commander to the command zone changes nothing; the next cast just
makes the counter go up. With partners, each has its own counter.

**Turn flow:**

1. **Setup**: choose on the play or on the draw, then draw 7.
2. **Mulligan** (London): "Mulligan" draws a new 7 and counts the mulligans.
   "Keep" asks you to click that many cards to put on the bottom. Commander's
   free first mulligan is a checkbox (on by default).
3. **Next turn** untaps everything, clears summoning sickness, resets the land
   drop, draws a card (no draw on turn 1 on the play), and starts a new log
   line.

**State and undo.** All game state is one plain JS object in the store on
`parentElement`. Every action pushes a copy onto an undo stack (a whole game
is a few hundred small objects, so copying is cheap), so Undo works for any
number of steps and restores the exact library order. "New hand" and "Reset"
start over; the undo stack is cleared by them.

**Shuffle.** `Math.random` with Fisher–Yates. There is no seed.

### Hints (Phase 3)

- **Mana available**: the sum of `amount` over untapped sources that aren't
  summoning sick, with their colors listed (`any` shows as ✱, null amounts as
  `?`).
- **Castable ✦**: mana value ≤ mana available, and the cost can be paid by an
  untapped set of sources. Colored pips are paired to **distinct** sources with
  a small bipartite match (a source with several options, like a dual land or a
  Talisman, can cover any one of them), then the sources left over must cover
  the generic part by their `amount`. A source with an amount above 1 (Sol
  Ring) can only pay generic. Hybrid pips accept either color, Phyrexian pips
  always pass, X counts as 0, and the commander's tax is added to its generic
  cost. If any untapped source has a null amount, the mark is withheld unless
  the spell is castable without it; if the cost is `null` there is no mark.

### Library actions (Phase 3)

- **Search library**: a filterable list of the library (name, type, a
  "basic lands" quick filter). The card you pick goes to your hand, or to the
  battlefield tapped or untapped. The library is shuffled after the search, as
  in the real game.
- **Look at top N / scry N**: shows the top N cards, and each one can be kept
  on top (order follows your clicks), put on the bottom, or sent to the
  graveyard (surveil).
- **Draw a card**: an extra draw for card-draw spells.

### Turn summary (Phase 4)

A small table under the log, kept in JS:

| Turn | Lands | Mana available | Mana value spent | Cast |
|---|---|---|---|---|
| 1 | 1 | 1 | 1 | Llanowar Elves |
| 2 | 2 | 3 | 1 | Sol Ring |
| 3 | 3 | 5 | 4 | Cultivate, Counterspell |

Below the table: "Commander cast on turn N", plus how much of the available
mana you used across the turns played. Turns with a `?` source show their
totals as a minimum.

## Edge cases the checks must cover

- **Library runs out.** Drawing from an empty library shows "Library is empty"
  in the log and a toast, draws nothing, and does not end the game. A mulligan
  or search on a short library works with what is there. A library of fewer
  than 7 cards shows a warning from the payload and still lets you play.
- **No commander matched.** The page shows a warning ("No card matched the
  commander on this deck; playing without a command zone") and plays the whole
  deck as a library. The match reuses the board's rule: front face name,
  lowercase. A double-faced commander is matched on its front face.
- **Two commanders (partner).** Both start in the command zone, each with its
  own tax.
- **A copy leaves the battlefield.** To hand, graveyard, exile or the library
  from ⋯, a card loses its tapped and summoning-sick marks and, if it was a
  chosen face, its face choice.
- **Lands-only or spells-only decks, and a one-card deck,** load without error.
- **Undo after a search or scry** restores the previous library order.

## Phases

### Phase 0 — Branch and plan

- Create the `goldfish` branch from `main` and commit this plan.

### Phase 1 — Payload and mana parsing

- `dashboard_lib/goldfish.py`: `goldfish_payload()`, `parse_cost()`,
  `mana_abilities()`, `enters_tapped()`, with a header comment in the same
  style as `deck_board.py`.
- `tests/test_goldfish.py`:
  - cards carry a `qty` and commander and partner come out of the library;
  - a no-match commander gives a warning; an empty deck gives empty lists;
  - `faces` for a one-face card, an MDFC with a land back (Boggart Trawler), a
    spell//spell (Consecrate // Consume), a Pathway-style double land, and a
    card with `mana_cost = NULL`;
  - `cost` for `{2}{G}{G}`, a hybrid, a Phyrexian, X, `{C}`, and a `null` one;
  - `mana` for a basic (1), a dual `Add {G} or {W}` (1, two colors), a triome
    (1, three colors), Command Tower (any), Sol Ring (2 colorless), a colored
    dork, Talisman (two abilities), Cabal Coffers (`amount: null`), and a non-mana
    creature (`null`). Each is read from its own face's text;
  - `enters_tapped` for a tapland (`always`), a check land and a shock land
    (`maybe`), a basic (`null`), and the back land of Hagra Mauling.
- Done when: tests pass. No UI yet.

### Phase 2 — Page and component, core play

- `dashboard_lib/components/goldfish.js` / `goldfish.css`, declared once at
  import like `_BOARD`, with the rules part and the drawing part kept
  separate and the game stored on `parentElement`.
- `dashboard_lib/goldfish.py`: `render_goldfish()` page wrapper (keyed
  `goldfish_<deck_id>`, no callbacks).
- `pages/11_Goldfish.py`: `card_view.setup_page`, the deck picker
  (`render_deck_picker`, `CURRENT_DECK_KEY`), deck header and links,
  `how_it_works` text, the warnings from the payload, and the component.
- `nav.PAGES` entry (Play section, 🐟, url_path `Goldfish`) and a
  `nav.DECK_LINKS` entry.
- Covers: zones, play/draw, opening hand, London mulligan, click to play, the
  two-face chooser, click to tap, the ⋯ menu, graveyard / exile lists, next
  turn, land-drop warning, enters tapped, summoning sickness, commander zone
  and tax, log, undo, reset, and the plain "Mana available" count with `?` (no
  castable marks yet).
- Tests that need a manual update, since `test_nav` and the page-wide smoke
  tests find the new file on their own: add `pages/11_Goldfish.py` with its
  picker key to `DECK_PICKERS` in `tests/test_pages_smoke.py`, and change the
  "10 pages" text in `PROJECT_STATE.md` (lines 8 and 112).
- Checks:
  - AppTest loads the page for a real deck.
  - esprima syntax check on the JS.
  - A headless Edge CDP script plays three turns on the play and on the draw
    and checks hand, library and battlefield counts, a mulligan to 6, tap /
    untap, undo, and the two-face chooser (play the land face, cast the spell
    face, cancel). The script also reruns the page (change an unrelated
    widget) and confirms the game is still there, then changes deck and
    confirms the game is gone. It calls the rules part directly through CDP
    `evaluate` for the library edge cases above.
  - **Check the database override before anything runs.** `db.DB_PATH` is a
    hardcoded constant ([db.py:12](../dashboard_lib/db.py#L12)), the repo keeps
    no browser-check harness, and a test write once reached the live database
    because of a bad `PYTHONPATH`. So the script lives in the session scratchpad
    (not the repo) with these rules, none of which are optional:
    - Copy `mtg_collection.db` to the scratchpad and point `PYTHONPATH` at a
      `sitecustomize.py` using a Windows-style path (`pwd -W`; an MSYS
      `/c/Users/...` path is silently ignored).
    - `sitecustomize.py` sets `dashboard_lib.db.DB_PATH` to the copy **and
      asserts it**, so a bad launch aborts instead of reading the live file.
    - Before the first page load, the script asks the running server which
      path it is using and stops if it isn't the copy.
    - Run Streamlit with `--server.address 127.0.0.1` on a free port, not 8765
      (the app's own port).
    - The page is read-only, so a stray write would also be a bug: after the
      run, compare the live database's size and modified time with its
      pre-run values.
    - Run the driver with `PYTHONIOENCODING=utf-8` and kill Edge with
      `taskkill /T /F`.

- **As built (2026-11-03):**
  - `render_goldfish(cards_df, deck_meta, deck_id, layer_tags=None)` shows the
    payload's warnings with `st.warning` and mounts the component keyed by
    `component_key(deck_id)`. The page adds a caption when the mainboard
    isn't 100 cards.
  - The store on `parentElement.__goldfish` holds `state`, `history`, `rules`
    (the rules part) and `act(name, ...args)`, which is what every click calls
    and what the browser check calls too. A refused action returns `{error}`,
    shows it as a toast and leaves no undo step.
  - Two buttons start over: **New hand** keeps the play/draw choice and draws
    7, **Reset** goes back to choosing. A tapped card is dimmed and turned 8°.
    A click outside an open menu only closes it; the click doesn't also play
    or tap the card under it.
  - `tests/test_pages_smoke.py`'s `_forget_deck_board()` now also drops
    `dashboard_lib.goldfish`, since it registers a component at import too.
  - Checks run: the full suite (954 passed), AppTest on all 12 real decks (on
    a copy), esprima, and a 38-item headless Edge run on a copy with the
    server's DB path checked first and the live file's size and mtime
    unchanged after.

### Phase 3 — Hints and library actions

- Mana colors in the readout, castable ✦ (commander included), search
  library, look at top N / scry / surveil, extra draw.
- Checks, through CDP `evaluate` on the rules part: castable matching for
  hybrid, Phyrexian, X, an `any` source covering the last pip, a dual land
  that has to be assigned to the right pip (Forest + Temple Garden for
  `{W}{G}`), Sol Ring paying only generic, too little mana, a `?` source, and
  a `null` cost; a scripted Cultivate-style search; scry keeps the order you
  chose; undo after a search restores the library order.

- **As built (2026-11-03):**
  - Rules added: `manaSources`, `manaAvailable` (now with `colors`),
    `canPay`, `castable`, `drawExtra`, `shuffleLibrary`, `searchLibrary`,
    `arrangeTop`.
  - **One change from the plan:** a source making more than 1 (Sol Ring) can
    pay one pip it covers, with the rest going to generic, instead of paying
    generic only. Sol Ring can pay `{C}{1}` that way. The rule still never
    claims more mana than is there; a `{G}{G}` source paying `{G}{G}` gets no
    ✦ (it's left out, not marked wrong). "Any color" never pays `{C}`. Each
    twobrid `{2/W}` is tried both as `{W}` and as 2 generic.
  - The readout lists one label per mana in WUBRG order, multicolor sources
    as `W/G`, then `✱` and `?`: `Mana available: 4 + ? (G C C W/G ?)`.
  - **Draw**, **Search** and **Look at top** sit next to the library count.
    Search groups copies (`Swamp ×18`), filters as you type without losing
    focus, and offers Hand · Battlefield · Battlefield tapped (the two-face
    chooser shows the non-spell faces only). Each pick shuffles the library,
    so a Cultivate is two picks. The panel stays open until Close, and its
    **Shuffle library** button covers a search that finds nothing. Searching
    doesn't use the land drop.
  - Look at top starts at 2 (− / + from 1 to 10). The choices are Top,
    Bottom, Graveyard and Hand (Hand covers impulse effects). Top and bottom
    follow the order you click, clicking a choice again clears it, and an
    undecided card stays on top after the chosen ones. Done applies it all as
    one undo step. It's refused if the top of the library changed underneath.
  - Checks run: the full suite (954 passed), AppTest on all 12 real decks (on
    a copy), esprima, and the headless Edge run grown to 81 items (Phase 2's
    38 + the castable matrix, readout, scripted Cultivate / scry / surveil,
    and the DOM: ✦ matches the rules on every hand card, search filter /
    basics / pick / undo, look order / undo), on a copy with the server's DB
    path checked first and the live file's size and mtime unchanged after.

### Phase 4 — Turn summary, docs

- Turn summary table and line.
- Docs: `dashboard_lib/components/README.md` (the goldfish component), the
  project `README.md` page list, `PROJECT_STATE.md`, and the `tests/README.md`
  entry. Move this plan to `docs/archive/` and add it to the archive index.
- Checks: summary totals match a scripted game, including a turn with a `?`
  source.

## Backlog (not planned)

- **Per-face costs and back-face art for two-faced cards.** Today the 10
  non-land multi-face cards get no ✦ and the chooser shows front art only,
  because `cards` flattens the faces (`mana_cost` is `NULL`, one image).
  The fix is additive: a `card_faces_json TEXT` column through the idempotent
  `ALTER TABLE` list in `queries.py`; fill it in `scryfall_lookup.to_card_row`
  and `card_resolver._insert_card_row` from Scryfall's `card_faces` (name, mana
  cost, image URL per face); backfill with Card Data Refresh (needs the
  network); then have `goldfish_payload()` use it instead of `null`. About half
  a day, and nothing else reads the column.

- "Deal 10 hands" quick stats: lands in hand and keepable hands, as a
  real-sample check on the Land Probability page's odds.
- Generic tokens and treasure / "+1 mana" markers.
- Counters on permanents.
- Damage dealt per turn, for aggro decks.
- Cards that make more than one land drop a turn (an "extra land drop" button).

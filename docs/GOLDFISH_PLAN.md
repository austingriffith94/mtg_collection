# Goldfish Plan — play out a deck's first turns by clicking

Working plan from the discussion of 2026-11-03. Goal: pick a deck, draw an
opening hand, and play out the first few turns by clicking cards to play
them and clicking permanents to tap them. Nothing is dragged. The page helps
you judge a deck's early game (land count, ramp, curve, and the turn you cast
your commander) by playing hands instead of reading odds.

Each phase ships on its own: the app runs, tests pass, and nothing is
half-converted at a phase boundary. Go in order. Work happens on the
`goldfish` branch, one commit per phase.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done

- [ ] **Phase 0** — Branch and plan.
- [ ] **Phase 1** — Deck payload and mana parsing (pure Python, tested).
- [ ] **Phase 2** — The Goldfish page and component: zones, turns, mulligan, tap, log, undo.
- [ ] **Phase 3** — Hints and library actions: castable marks, search, scry, extra draw.
- [ ] **Phase 4** — Turn summary, seeded replay, docs and wrap-up.

## Decisions

Agreed 2026-11-03:

- **Click, don't drag.** You click a hand card to play it and click a
  battlefield card to tap or untap it. Rarer moves are in a small ⋯ menu on
  the card.
- **The game runs in the browser.** It is a `st.components.v2` component,
  built like the deck board (`dashboard_lib/components/`, JS and CSS read at
  import time, no npm or build step). Shuffling, drawing, tapping and turn
  changes never cause a Streamlit rerun. A rerun per click would reload every
  card image and feel slow.
- **Python sends the deck once.** `goldfish_payload()` is pure and
  unit-tested. After the page sends it, Python takes no part in play.
- **Read-only.** The page never writes to the database. A goldfish run leaves
  no trace once you close the page (saving notes from a run is in the backlog).
- **Its own page**: `pages/11_Goldfish.py`, in the **Play** section of
  `nav.PAGES`. It is also added to `nav.DECK_LINKS`, so every per-deck page
  (Decks, Deck Editor, Probabilities, Advisor) links to it. It follows
  `card_view.CURRENT_DECK_KEY` like the others.
- **Mana is manual, with hints.** You tap your own sources. The page shows
  the mana your untapped sources can make and marks the cards you could cast
  with it, but it never pays for a spell for you. Automatic payment gets the
  edge cases wrong (filter lands, hybrid costs, X spells) and then the hints
  can't be trusted.
- **Rules are shown, not enforced.** A second land drop in a turn shows a
  warning but is allowed. Summoning sickness, enters-tapped lands and
  commander tax are tracked so the hints stay honest, but nothing is blocked.
  Goldfishing is practice, and some cards break the normal rules (extra land
  drops, haste).
- **No life, combat or stack.** You're playing alone, so there's nothing to
  attack. Damage dealt per turn is in the backlog if you want it later.
- **Mainboard only.** Commander(s) start in the command zone and the rest of
  `deck_cards` makes up the library. The maybeboard and shortlist are left out.
  A deck that isn't 100 cards (a brewing deck) can still be played, with a
  caption giving its card count.

## The design

### Payload (`dashboard_lib/goldfish.py`)

`goldfish_payload(cards_df, deck_meta, layer_tags=None)` takes the same
inputs as `deck_board.board_payload` (`loaders.load_deck_cards_df`,
`loaders.load_deck_meta`) and returns
`{"commanders": [...], "library": [...]}`. **Each copy is its own entry**:
30 Forests become 30 cards with ids `"<scryfall_id>#1"` … `"#30"`, because
each copy can be tapped separately.

Each card dict holds:

| Field | Source | Used for |
|---|---|---|
| `id`, `sid`, `name`, `img` | row, `card_view.card_image_src` | display, hover preview |
| `cmc`, `mana_cost` | row | castable check, summary, cost shown on the card |
| `type`, `is_land` | `fmt.derive_card_type`, `fmt.has_land_face` (MDFC-aware) | where the card goes when played |
| `is_spell_only` | instant or sorcery front face | goes to the graveyard after casting |
| `is_creature` | front face type | summoning sickness for dorks |
| `mana` | see below, or `null` | mana readout |
| `enters_tapped` | `"always"` / `"maybe"` / `null` | land enters tapped, or carries a "may enter tapped" badge |
| `mdfc_land_back` | back face is a land, front is not | asks "play as land or cast?" |
| `tags` | `strategy` layer (+ `layers` like the board) | small role badges (Ramp, Draw…) |
| `is_commander` | `deck_meta` commander / partner | command zone |

**`mana`** is `{"colors": [...], "any": bool, "amount": n}`, or `null` for a
card that makes no mana. It is built from `fmt.classify_mana_colors` for lands
and for `fmt.is_mana_rock_or_dork` cards, the same rules the Land Probability
page uses. `amount` counts the mana symbols in the first "Add …" clause, so
Sol Ring is 2 and most cards are 1. Anything the parser can't read counts as 1.
This is a hint, so it can be a little off for rare cards; the tests pin down
the common ones.

**`enters_tapped`** uses a regex on the oracle text. "enters tapped" or
"enters the battlefield tapped" with no condition gives `"always"`. Any
conditional version (unless / if you control …) gives `"maybe"`: the card
enters untapped and carries a badge, and you click to tap it if the condition
applies.

### The component (`dashboard_lib/components/goldfish.js` / `.css`)

Layout, top to bottom:

```
┌ Turn 3 · On the play · Land drop: ✓ used ─────── [Next turn ▶] [Undo] [Reset] ┐
│ Mana available: 4  (G G U C)     Library 89 · Graveyard 1 · Exile 0           │
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
| Hand card, land | to Lands (tapped if `enters_tapped == "always"`); uses the land drop |
| Hand card, permanent | to Permanents (creatures marked summoning sick this turn) |
| Hand card, instant / sorcery | to Graveyard, logged as cast |
| Hand card, MDFC with a land back | asks "Land or spell?" first |
| Battlefield card | tap / untap |
| Commander | cast to Permanents; back to the command zone from ⋯ adds 2 tax |
| ⋯ on any card | to hand · graveyard · exile · top / bottom of library |
| Graveyard / Exile count | opens the list; ⋯ works there too |
| Hover | full card preview, as on the deck board |

**Turn flow:**

1. **Setup**: choose on the play or on the draw, then draw 7.
2. **Mulligan** (London): "Mulligan" draws a new 7 and counts the mulligans.
   "Keep" asks you to click that many cards to put on the bottom. Commander's
   free first mulligan is a checkbox (on by default).
3. **Next turn** untaps everything, clears summoning sickness, resets the land
   drop, draws a card (no draw on turn 1 on the play), and starts a new log
   line.

**State and undo.** All game state lives in one plain JS object. Every action
pushes a copy onto an undo stack (a whole game is a few hundred small
objects, so copying is cheap), so Undo works for any number of steps. The
state is mirrored back with `setStateValue`, so an unrelated rerun of the page
(the sidebar, the deck picker) does not lose the game. Phase 2 checks that
`setStateValue` doesn't itself trigger a rerun. If it does, the mirror is
written only on "Next turn".

**Shuffle.** A seeded PRNG (mulberry32) plus Fisher–Yates. The seed is shown
next to Reset, which is what Phase 4's replay builds on.

### Hints (Phase 3)

- **Mana available**: the sum of `mana.amount` over untapped sources that
  aren't summoning sick, with their colors listed (`any` shows as ✱).
- **Castable ✦**: mana value ≤ mana available, and each colored pip in
  `mana_cost` can be covered by a distinct untapped source of that color
  (greedy: single-color sources first, then `any`). Hybrid pips accept either
  color, Phyrexian pips always pass, and X is counted as 0. The commander gets
  the mark too, with tax included.

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
mana you used across the turns played.

**Replay this hand** restarts with the same seed, so you can play the same
draws a different way. **New hand** picks a new seed.

## Phases

### Phase 0 — Branch and plan

- Create the `goldfish` branch from `main` and commit this plan.

### Phase 1 — Payload and mana parsing

- `dashboard_lib/goldfish.py`: `goldfish_payload()`, `mana_production()`,
  `enters_tapped()`, with a header comment in the same style as
  `deck_board.py`.
- `tests/test_goldfish.py`:
  - copies are split out one per entry, and commander and partner are taken
    out of the library;
  - MDFC lands and `mdfc_land_back`;
  - `mana` for a basic, a dual, a triome, Command Tower (any), Sol Ring
    (2 colorless), a colored dork, a non-mana creature (`null`);
  - `enters_tapped` for a tapland, a check land / fast land (`maybe`), a basic
    (`null`);
  - an empty deck returns empty lists.
- Done when: tests pass. No UI yet.

### Phase 2 — Page and component, core play

- `dashboard_lib/components/goldfish.js` / `goldfish.css`, declared once at
  import like `_BOARD`.
- `dashboard_lib/goldfish.py`: `render_goldfish()` page wrapper.
- `pages/11_Goldfish.py`: `card_view.setup_page`, the deck picker
  (`render_deck_picker`, `CURRENT_DECK_KEY`), deck header and links,
  `how_it_works` text, and the component.
- `nav.PAGES` entry (Play section, 🐟, url_path `Goldfish`) and a
  `nav.DECK_LINKS` entry.
- Covers: zones, play/draw, opening hand, London mulligan, click to play,
  click to tap, the ⋯ menu, graveyard / exile lists, next turn, land-drop
  warning, enters tapped, summoning sickness, commander zone and tax, log,
  undo, reset, and the plain "Mana available" count (no castable marks yet).
- Checks: `test_nav` and `test_pages_smoke` pick up the page; AppTest loads it
  for a real deck; a headless Edge CDP script against a DB copy (see the
  browser-check notes) plays three turns on the play and on the draw and
  checks hand, library and battlefield counts, a mulligan to 6, tap / untap,
  undo, and that a page rerun keeps the game; the JS is syntax-checked with
  esprima.

### Phase 3 — Hints and library actions

- Mana colors in the readout, castable ✦ (commander included), search
  library, look at top N / scry / surveil, extra draw.
- Checks: JS unit checks through CDP `evaluate` on the castable function
  (hybrid, Phyrexian, X, an `any` source covering the last pip, too little
  mana); a scripted Cultivate-style search; scry keeps the order you chose.

### Phase 4 — Turn summary, replay, docs

- Turn summary table and line, Replay this hand / New hand with the seed shown.
- Docs: `dashboard_lib/components/README.md` (the goldfish component),
  the project `README.md` page list, `PROJECT_STATE.md`, and the `tests/README.md`
  entry. Move this plan to `docs/archive/` and add it to the archive index.
- Checks: replaying a seed gives the same opening hand and draws; summary
  totals match a scripted game.

## Backlog (not planned)

- "Deal 10 hands" quick stats: lands in hand and keepable hands, as a
  real-sample check on the Land Probability page's odds.
- Save notes from a goldfish run to the deck (would be the page's first
  write; needs its own decision).
- Generic tokens and treasure / "+1 mana" markers.
- Counters on permanents.
- Damage dealt per turn, for aggro decks.
- Cards that make more than one land drop a turn (an "extra land drop" button).

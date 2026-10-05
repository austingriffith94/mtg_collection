# Implementation Plan — Swap Rethink, Workbench, and Reconciliation

Working spec for the five-point rework agreed on 2026-10-04. Each phase
below is independently shippable: the app runs, tests pass, and nothing is
half-migrated at any phase boundary. Work them in order — Phase 1 depends
on Phase 0's table, and Phases 2-5 all read it.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done

- [x] **Phase 0** — `deck_changes` table + backfill *(done 2026-10-04: 187 ideas + 44 planned = 231 rows backfilled, legacy tables intact, 10 tests)*
- [x] **Phase 1** — Unified Shortlist (replaces Maybeboard + Swap Manager tabs) *(done 2026-10-04: 61 tests passing, page driven end to end against a copy of the live DB — see "Phase 1 as built")*
- [x] **Phase 2** — Workbench page: global pending queue, queue-aware inventory *(done 2026-10-04: 119 tests passing, page driven against a copy of the live DB — see "Phase 2 as built")*
- [x] **Phase 3** — Contention table, buy list, Card Lookup page *(done 2026-10-04: 130 tests passing, both pages driven against a copy of the live DB — see "Phase 3 as built")*
- [x] **Phase 4** — Reconciliation: unlocated lots, list-vs-physical diffs *(done 2026-10-04: 141 tests passing, page driven against a copy of the live DB — see "Phase 4 as built")*
- [x] **Phase 5** — Change history, deck compare matrix, home-tile status strips *(done 2026-10-04: 155 tests passing, home + Decks pages driven against a copy of the live DB, all ten pages render — see "Phase 5 as built")*
- [x] **Phase 6** — Deck lifecycle: brewing a new deck, dismantling/converting an old one *(done 2026-10-04: 190 tests passing, all ten pages render, Build Plan + Dismantle controls driven against a copy of the live DB — see "Phase 6 as built")*

---

## Why this rework

Observed in `mtg_collection.db` on 2026-10-04, which is what motivated each
phase:

| Observation | Phase |
|---|---|
| 187 maybeboard rows, 100% already owned, only 6 with a resolved replace target — the column exists but nothing consumes it | 0, 1 |
| 44 queued swaps across 10 decks, all queued in one sitting on 2026-10-03, none applied, invisible outside Deck Editor → pick deck → Swap tab | 1, 2 |
| `execute_swap()` mutates `deck_cards`/`collection` and clears the queue, leaving zero trace — 29 games logged since 2021 can't be correlated to any decklist change | 0, 5 |
| 178 collection lots have NULL location (103 rares + 22 mythics, ≈$1.57k); 132 of 187 maybeboard candidates resolve to one of them, so "can I grab it?" answers *unknown* ~70% of the time | 4 |
| "A Refined Grizzly of Noble Heritage" is a 100-card list with 3 lots located to it — on paper, not built, and nothing says so | 4 |
| 96 oracle cards sit in 2+ mainboards; 39 non-basics are over-subscribed (`War Room` 4 decks/3 copies, `Duelist's Heritage` 3/2, `Blood Crypt` 3 maybeboards/1 copy) | 3 |
| "A Refined Grizzly" is a brew with no workflow: of its 85 non-basics, 54 are unlocated, 10 are in the box, and **17 have their only copy sleeved in another deck** — 8 of those in Tuvasa alone | 6 |

Design decisions taken up front, so they don't get re-litigated mid-build:

1. **One lifecycle table**, not three tables holding one workflow. The
   maybeboard, the swap queue, and the change log are the same row at three
   points in its life.
2. **A new Workbench page** owns the cross-deck "what should I do next"
   views. Collection and Decks stay browse surfaces.
3. **Unowned ideas are first-class** — flagged, priced, and rolled up into a
   cross-deck buy list. A shortlist doubles as a shopping list.
4. **Brewing and dismantling are change-sets, not special cases** — both
   compile down to `deck_changes` rows, so they inherit the allocator, the
   preview-before-apply step, and the history for free (Phase 6).

---

## Phase 0 — `deck_changes` table + backfill

**Goal:** one table that holds an idea, a staged swap, and an applied change,
backfilled from existing data so nothing is lost and nothing is retyped.

### Schema

Added through the existing additive-upgrade path, *not* a re-migration — see
`_SCHEMA_TABLE_UPGRADES` in [`dashboard_lib/queries.py:95`](dashboard_lib/queries.py#L95)
and the policy in README.md ("Schema changes without re-migrating").

```sql
CREATE TABLE deck_changes (
    change_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id             INTEGER NOT NULL REFERENCES decks(deck_id),
    status              TEXT NOT NULL DEFAULT 'idea'
                        CHECK(status IN ('idea','planned','applied','dropped')),
    -- The card going IN. NULL for a pure removal.
    add_scryfall_id     TEXT REFERENCES cards(scryfall_id),
    add_name            TEXT,              -- free-text fallback if resolution failed
    -- The card coming OUT. NULL while still an unpaired idea.
    remove_scryfall_id  TEXT REFERENCES cards(scryfall_id),
    remove_name         TEXT,              -- free-text fallback
    quantity            INTEGER NOT NULL DEFAULT 1,
    review_flag         BOOLEAN DEFAULT 0, -- carried over from maybeboard
    notes               TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    planned_at          TIMESTAMP,         -- set when idea -> planned
    resolved_at         TIMESTAMP,         -- set when -> applied or dropped
    -- A row must do something: add a card, remove one, or both.
    CHECK(add_scryfall_id IS NOT NULL OR add_name IS NOT NULL
          OR remove_scryfall_id IS NOT NULL)
);
CREATE INDEX idx_deck_changes_deck_status ON deck_changes(deck_id, status);
```

The four statuses and what each means:

| Status | Means | Was previously |
|---|---|---|
| `idea` | A card I'm considering for this deck. May or may not name what it would replace. May or may not be owned. | `maybeboard` row |
| `planned` | Paired with a card to cut and staged for execution. Nothing written to the deck yet. | `deck_swap_queue` row |
| `applied` | Executed against `deck_cards`/`collection`. Immutable history. | *nothing — lost* |
| `dropped` | Considered and rejected. Kept so it isn't reconsidered from scratch. | *nothing — deleted* |

`dropped` matters more than it looks: deleting a rejected idea means
re-evaluating the same card next month with no memory of why it lost.

### Backfill

A seed function in the `_SCHEMA_TABLE_UPGRADES` tuple, which runs exactly
once — the first time the table is created on a given database — matching how
`theme_catalog` and `location_catalog` seeded themselves:

- Each `maybeboard` row → `status='idea'`, carrying `review_flag`, `notes`,
  and `replace_scryfall_id`/`replace_card_name` into the remove columns.
  A row *with* a replace target stays `idea`, not `planned`: it was never
  staged, and silently promoting 6 rows into an execution queue would be a
  surprise.
- Each `deck_swap_queue` row → `status='planned'`, with `queued_at` copied
  into **both** `created_at` and `planned_at`.

Expected on the live database: 187 `idea` + 44 `planned` = 231 rows.

### Legacy tables

`maybeboard` and `deck_swap_queue` are left in place and untouched, per the
project's non-destructive column/table policy — they become a frozen backup
of the pre-rework state. After Phase 1 nothing writes to them, and nothing
should read them either.

> **The trap to avoid:** six places read `maybeboard` today. If Phase 1 adds
> a new UI without repointing all of them, Moxfield exports silently drop
> every idea and `prune_collection()` starts deleting lots that ideas depend
> on. Full list in Phase 1.

### Tests (`tests/test_deck_changes.py`)

- Table is created by `ensure_schema()` on a database that lacks it.
- Backfill maps a maybeboard row (with and without a replace target) and a
  swap-queue row to the right status and timestamps.
- Backfill is idempotent — a second `ensure_schema()` adds no rows.
- `CHECK` rejects a row that neither adds nor removes anything.

---

## Phase 1 — Unified Shortlist

**Goal:** the maybeboard *is* the swap queue. Promote an idea to a staged
swap in one click, never retyping a card name.

### UI

Deck Editor's `tab_maybe` ([`pages/4_Deck_Editor.py:491`](pages/4_Deck_Editor.py#L491))
and `tab_swap` ([`pages/4_Deck_Editor.py:576`](pages/4_Deck_Editor.py#L576))
collapse into one **Shortlist** tab with three sections:

```
Shortlist — <deck name>

[+ Add an idea]  name ___________  set __  num __   replaces [dropdown ▾]  notes ______
                                                    (replaces is optional)

── Ideas (N) ──────────────────────────────────────────────────────────
  Card              Owned  Where            Replaces           Review  Notes
  Blood Crypt       x1     Box              Sulfurous Springs    [x]   ...   [→ Plan] [Drop]
  Jeweled Lotus     —      NOT OWNED  $42   (pick one ▾)         [ ]   ...   [→ Plan] [Drop]

── Planned (N) ────────────────────────────────────────────────────────
  Add               Out                Qty  Availability
  Undead Warchief   Heirloom Blade      1   Available in Box          [← Idea] [Drop]
  Ripples of Undeath Night's Whisper    1   ⚠ claimed by Marchesa     [← Idea] [Drop]
                                            (planned swap #12)
  [✅ Apply all planned]   [Apply checked only]

── History (N) ────────────────────────────── (collapsed expander)
  applied / dropped rows, newest first
```

Key behaviours:

- **`→ Plan`** is the one-click promotion. It requires a remove target; if the
  idea already names one, the click is sufficient. If not, the row's
  `Replaces` dropdown (current mainboard, same control as today's Swap
  Builder) must be set first. This is the fix for the unused
  `replace_scryfall_id` column — something finally consumes it.
- **`← Idea`** demotes without losing the pairing, for when a plan isn't
  ready.
- **`Drop`** sets `status='dropped'`, never deletes.
- **Display the resolved name, not the raw text.** All 6 backfilled ideas
  that name a replacement stored it lowercase (`credit voucher`, `swamp`,
  `temple of plenty`) while resolving correctly to a real card. Render
  `cards.name` via `remove_scryfall_id`, falling back to `remove_name` only
  when the id is NULL. Same for the add side.
- **Owned / Where / price** columns come from `queries.card_inventory_status()`,
  extended per below. An unowned idea shows `NOT OWNED` + current price —
  ideas no longer need to be cards you own.
- Apply is **per-row selectable**, not just all-or-nothing. With 9 planned
  swaps on one deck, "apply the 3 I'm sure about" is the common case.

### `writes.py` changes

New, replacing the maybeboard + swap-queue function pairs:

```
add_change(conn, deck_id, *, add_scryfall_id=None, add_name=None,
           remove_scryfall_id=None, remove_name=None, quantity=1,
           review_flag=0, notes=None, status='idea')
update_change(conn, change_id, **fields)
promote_change(conn, change_id, remove_scryfall_id, remove_name, quantity=1)
demote_change(conn, change_id)
drop_change(conn, change_id)
bulk_update_changes(conn, deck_id, updates)   # data_editor save path
apply_change(conn, change_id)                 # wraps execute_swap
apply_changes(conn, change_ids)               # batch, ordered, allocator-aware
```

`execute_swap()` ([`dashboard_lib/writes.py:883`](dashboard_lib/writes.py#L883))
keeps its current body — it already does the right thing (update `deck_cards`,
move the cut card's lot back to storage, sleeve the added card from an
available lot or create a starter one). Phase 1 wraps it so that on success it
sets `status='applied'` and `resolved_at` **in the same transaction** rather
than clearing a queue row. That single change is what gives Phase 5 its
history for free.

Also repoint `add_deck_card`/`remove_deck_card`/`bulk_update_deck_cards`
([`writes.py:507-543`](dashboard_lib/writes.py#L507)) to write an `applied`
`deck_changes` row, so hand edits appear in history too, not just swaps.

### Every reader of `maybeboard` that must be repointed

Miss one of these and something breaks silently:

| Location | What it does | Change |
|---|---|---|
| [`queries.py:704`](dashboard_lib/queries.py#L704) `maybeboard_dataframe` | powers the Decks page browser | new `deck_changes_dataframe(conn, deck_id, statuses=...)`; keep old fn as a thin shim until Phase 2 |
| [`queries.py:1200`](dashboard_lib/queries.py#L1200) summary count | home-page caption | count `status='idea'` |
| [`loaders.py:53`](dashboard_lib/loaders.py#L53) `load_maybeboard_df` | cache wrapper | → `load_deck_changes_df`, added to `invalidate_deck_caches()` |
| [`moxfield_export.py:50-60`](dashboard_lib/moxfield_export.py#L50) | `// Maybeboard` export section | read `status='idea'` — **otherwise exports silently lose all 187 ideas** |
| [`writes.py:672`](dashboard_lib/writes.py#L672) `prune_collection` | protects maybeboard cards from pruning | must protect `idea`/`planned` cards — **otherwise pruning deletes lots your ideas depend on** |
| [`writes.py:433`](dashboard_lib/writes.py#L433) `delete_deck` cascade | deletes deck's child rows | add `deck_changes` to the list |
| [`pages/2_Decks.py:682`](pages/2_Decks.py#L682) Board toggle | Mainboard / Maybeboard / Both | relabel to Mainboard / Shortlist / Both |
| [`dashboard.py:97`](dashboard.py#L97) | maybeboard-rows caption | ideas + planned counts |
| [`refresh.py:14`](dashboard_lib/refresh.py#L14) comment | stale doc reference | update text |

### Tests (`tests/test_deck_changes.py`)

- `promote_change` sets `planned_at` and refuses a promotion with no remove
  target.
- `apply_change` updates `deck_cards`, moves the lot, **and** leaves one
  `applied` row — assert all three in one test, since the point is that they
  are atomic.
- A failed apply (unresolvable card name) leaves the row `planned`, not
  half-applied.
- `prune_collection` does not delete a lot referenced by an `idea`.
- Moxfield export includes `idea` rows under `// Maybeboard`.

### Phase 1 as built

Where the build differs from, or adds to, the spec above. Read this before
starting Phase 2.

**A tenth `maybeboard` reader the spec missed:** `scripts/sync_images.py`
(`prune_orphaned`) kept cached card art for anything on a maybeboard. Left
alone it would have deleted the art for every new idea. Repointed. It also
shows why the reader list should have been generated by `grep`, not recalled.

**The `NOT IN (NULL)` trap was real.** `prune_collection()` now protects open
shortlist cards with `NOT IN (SELECT add_scryfall_id ... AND add_scryfall_id
IS NOT NULL)`. Without the `IS NOT NULL`, one unlinked row (all 44 backfilled
swaps have no `add_scryfall_id`) makes the condition NULL and silently
switches pruning off for the whole collection. Mutation-checked: removing the
filter fails `test_an_unlinked_change_does_not_switch_pruning_off`.

**`delete_deck()` still clears `maybeboard` and `deck_swap_queue`.** They are
frozen backups nothing writes to, but their rows still carry a `deck_id` FK,
so leaving them out would make deleting a deck fail under `foreign_keys=ON`.

**Hand edits are logged, as specced, but only presence changes.** `add_deck_card`
/ `remove_deck_card` write an `applied` row when a card genuinely joins or
leaves the list; a quantity bump on an existing row (Swamp x4 -> x5) does not.
`execute_swap` goes through non-logging `_put_deck_card` / `_delete_deck_card`
so a swap is ONE history row, not an add plus a remove. Mutation-checked.

**Apply is not atomic** (the spec said "same transaction"). `execute_swap`
commits as it goes; rewriting it to be transactional was out of scope. Instead
`apply_change` runs every check that can fail *before* touching anything, and
refuses a swap whose cut card has left the mainboard (which would otherwise
add a card without cutting one and record a false `applied` row). The residual
risk, an unexpected database error mid-swap, is documented in its docstring.

**Quantity semantics are unchanged and now surfaced.** `execute_swap` replaces
the cut card's *whole* deck row, so cutting a basic at x5 for one card removes
all five. The Planned table shows a warning when applying everything would
change the deck's card count.

**Decks page browser omits unlinked planned rows.** The browser needs a real
printing to render a tile, so `shortlist_dataframe` inner-joins the add card;
the 44 backfilled swaps (typed name only) appear on the Deck Editor's
Shortlist tab but not in the Decks-page Shortlist board until they are
applied (apply links the printing). Phase 2 should decide whether to link them
proactively with a local name match.

**Not done, deliberately:** a stale-idea cleanup. An idea whose card is later
added to the mainboard by hand stays on the shortlist. Cheap to add (auto-drop
or flag on `add_deck_card`), but it changes what "drop" means, so it wants a
decision rather than a default.

**Verified how:** `tests/test_deck_changes_writes.py` (43 tests); the real Deck
Editor page driven through `AppTest` against a copy of the live database
(apply, promote, drop, restore, add, duplicate refusal, the no-target error);
the Decks page with all three Board options; Moxfield export counts matching
the database; a render sweep of every page; and the two old offline scripts
that grep page source (`_test_prompt_pass13_offline.py`,
`_test_spellbook_offline.py`) updated for the retired UI.

---

## Phase 2 — Workbench page: global queue + queue-aware inventory

**Goal:** pending work is visible without hunting, and the inventory check
stops lying when two plans want the same physical card.

### The allocator (the actual fix for point 2)

Today `card_inventory_status()`
([`queries.py`](dashboard_lib/queries.py)) is called **once per row against
current DB state**, so two planned swaps that both want your single
`Blood Crypt` each independently report "Available in storage." Neither is
wrong alone; together they're impossible.

Replace the per-row check with a single pass over all planned rows:

```
queries.plan_availability(conn, changes) -> {change_id: {...}}
```

- Build a working pool of available lots per `oracle_id` (not per `cards.name`
  — see the bug note below).
- Walk `changes` in a deterministic order (`planned_at`, then `change_id`),
  decrementing the pool as each row claims a copy.
- Each row gets one verdict: `available` (with location) · `claimed_by`
  (naming the earlier change and its deck) · `in_other_deck` (naming decks
  holding every copy) · `not_owned` (a starter lot will be created) ·
  `already_here`.

Called with one deck's rows from the Shortlist, and with *every* deck's rows
from the Workbench — same function, so the two views can never disagree.

> **Bug to fix while in here:** `card_inventory_status()` matches on
> `cards.name COLLATE NOCASE`, not `oracle_id`. Across 12 decks, MDFC names
> like `Malakir Rebirth // Malakir Mire` (in 3 decks) and
> `Witch Enchanter // Witch-Blessed Meadow` (2 decks, 1 copy) are a live
> sharp edge, and a different printing of the same card is a near-miss by
> design. Match on `oracle_id` with a name fallback for unresolved rows.

### Page: `pages/8_Workbench.py`

```
🔧 Workbench
"44 planned changes across 10 decks · 3 conflicts · 178 lots unlocated"

── Conflicts (3) ─────────────── shown first; these are the blocking ones
  Blood Crypt — 1 owned, wanted by: Raktres (planned #7), Marchesa (planned #12),
                                     Prosper's (idea)
  [resolve: keep in ▾]  [drop others]

── Planned changes, by deck ─────────────────────────
  ▸ Gisa, Zombie Gardener (9)      [Apply all]  [Apply checked]
  ▸ Raktres, Lord of Discounts (9) [Apply all]  [Apply checked]
  ...
  (each expander: the same planned-rows table as the Shortlist tab)

── Ideas needing a decision (19) ──── review_flag = 1, across all decks
── Buy list → Phase 3 ───────────────
── Reconcile → Phase 4 ──────────────
```

Conflicts lead because they're the rows that *cannot all* succeed. Everything
else is just a to-do list.

### Tests (`tests/test_plan_availability.py`)

- Two planned rows in different decks wanting one owned copy: first
  `available`, second `claimed_by` naming the first.
- Same, within one deck.
- Three decks wanting 2 owned copies → two `available`, one `claimed_by`.
- An MDFC name resolves by `oracle_id` (regression test for the match bug).
- Ordering is deterministic across repeated calls.

---

### Phase 2 as built

Where the build differs from, or adds to, the spec above. Read this before
starting Phase 3.

**A bigger bug than the one the spec sent me in for.** The spec called out
`card_inventory_status()` matching on `cards.name` instead of `oracle_id`.
Fixed — but on the live data that changes *nothing*: all 1,885 cards have an
`oracle_id`, and no open shortlist row currently names an MDFC face
(the MDFCs are in mainboards, not shortlists). It's covered by tests and it
matters for `execute_swap`, but it wasn't what was breaking.

What *was*: `collection.quantity` is nullable, and the schema says NULL means
"untracked / bulk". **183 of the 1,472 lots are NULL.** The old check read
that as `quantity or 0`, so a card physically sitting in a box reported
`owned_qty = 0` → **NOT OWNED**. With the whole queue allocated, that was
146 of 231 rows calling for a purchase. `_lots_for_card()` now COALESCEs to
1, and the picture flips to what the plan's own observations predicted:

| | before the fix | after |
|---|---|---|
| `not_owned` | 146 | 7 |
| `available` | 62 | 190 |
| `claimed_by` | 3 | 14 |
| conflicts | 14 (all phantom: `owned=0 free=0`) | 11 (real: Blood Crypt `owned=1 free=1 wanted=3`) |

Mutation-checked: reverting the COALESCE fails three tests. **Phase 3's buy
list reads `not_owned`, so without this it would have quoted you a 146-card
shopping list for cards you already own.**

**Bulk basics are unlimited.** COALESCE-to-1 was wrong for basic lands, where
NULL quantity means "untracked bulk": Mountain and Swamp showed as phantom
conflicts. `queries.is_bulk_basic()` makes a basic with any NULL-quantity lot
always `available` (flagged `bulk`), claiming nothing and never in a
conflict. Non-basics with a NULL lot stay at 1; a basic with all-tracked
quantities is still limited. Live data after: 9 conflicts (was 11), all
real. Other `SUM(quantity)` readers (Game Changers / Mana Tags "owned qty",
`collection_summary`) still skip NULL lots; display-only, left alone.

**14 rows the old per-row check got wrong.** Measured directly against the
live data: 14 open changes that `card_inventory_status()` called available
are, under allocation, already claimed by an earlier change. That number is
the phase's actual deliverable.

**`already_here` claims nothing.** A fifth verdict beyond the spec's five
(plus `no_add` for a removal-only row). An idea for a card the deck already
has is a no-op — and if it claimed a copy anyway, it would deny one to a deck
that actually needs it. Mutation-checked.

**Supply is copies, not lots.** A single lot of 2 satisfies two changes.
Worth stating because the pool is built *from* lots.

**A partial claim is reported by its blocker.** A row wanting 3 copies that
gets 1 comes back `claimed=1, wanted=3` with the verdict for the 2 it
didn't get, because that shortfall is the actionable half.

**Conflict resolution lives in `writes`, not the page.** `st.data_editor`
checkbox state can't be driven from `AppTest`, so the keep-drops-the-rest
rule would have been untestable inline. It's `writes.resolve_contention()`
instead, with the guards that matter: nothing is dropped unless something
was asked for, a keep that isn't one of the competing rows is refused, and a
row another tab resolved since the page rendered is *reported*, not raised,
so the rest of the conflict still resolves. 8 tests, 4 mutants caught.

**Stale ideas are auto-dropped** (the decision Phase 1 left open).
`add_deck_card()` retires a deck's open rows for a card added to its
mainboard by hand, stamping `writes.AUTO_DROP_NOTE` into the notes so
History distinguishes "I decided against this" from "I just did it by
hand". Matched across printings, so an idea for the cheap printing is
satisfied by hand-adding the foil. Not marked `applied`, because
`add_deck_card` already logs an `applied` row for the hand edit and one real
event should be one history row. `reopen_change()` is the escape hatch. Only
a genuine join triggers it — a quantity bump doesn't, and `execute_swap`
goes through the non-logging `_put_deck_card`, so applying a planned swap
never auto-drops its own row. Mutation-checked (4 mutants).

**The Workbench's Qty column is read-only.** Changing it changes how many
cards a swap cuts; that needs the Shortlist tab's mainboard context and its
card-count warning to decide safely.

**Ideas are allocated alongside planned rows, not after them.** An idea
competes for a physical card just as much as a staged swap does. Order is
`(planned_at or created_at, change_id)`, so a swap staged last month
outranks an idea jotted yesterday — and a planned row outranks an idea by
construction, since an idea has no `planned_at`.

**Not done, deliberately:** the contention table and buy list are Phase 3;
this page shows their counts as stubs so the shape is visible. Reconcile is
Phase 4, same treatment.

**Verified how:** `tests/test_plan_availability.py` (37 tests) and 19 new
tests in `tests/test_deck_changes_writes.py` (116 total, all passing); 13
mutants introduced and all 13 caught; the Workbench driven through `AppTest`
against a copy of the live database — every guard path, "Apply all for this
deck" applying 9 real swaps (`deck_cards` net 0, one new starter lot, 9
`applied` rows), the flagged-ideas table, and the empty state; the allocator
run over all 231 live open rows; and a render sweep of all nine pages.

---

## Phase 3 — Contention, buy list, Card Lookup

### Contention table (Workbench section)

Demand vs. supply per oracle card, non-basics only, shortfall descending:

```sql
demand   = (# decks with the card in deck_cards)
         + (# decks with an idea/planned add for it)
owned    = SUM(COALESCE(collection.quantity, 1))
shortfall = demand - owned        -- shown where > 0
```

On today's data that's 39 rows, led by `Blood Crypt` (1 owned, 3 shortlists),
`War Room` (3 owned, 4 mainboards), `Duelist's Heritage` (2 owned, 3
mainboards). Each row expands into which decks, and offers *buy another copy*
(→ buy list) or *drop from deck N*.

Implement as a function in `queries.py` rather than a SQL view: the
`deck_changes` half of the demand needs status filtering that'll change as
Phase 5 adds history, and a view would freeze it.

### Buy list (Workbench section)

Ideas where `owned_qty = 0`, grouped by card across decks, with
`current_price_usd` (all 1,885 cards have one, synced 2026-10-03) and a
running total. Plus contention rows where buying a second copy resolves the
shortfall — those are the "you already decided you want this twice" buys.
Export as plain text, matching the existing Moxfield-export pattern in
[`moxfield_export.py`](dashboard_lib/moxfield_export.py).

### Page: `pages/9_Card_Lookup.py`

One card → everything about it. The view the data shape keeps asking for and
no page currently answers:

```
🔍 Card Lookup        [search by name ▾]

  [card image]   Sulfurous Springs            $6.42 · rare · ONE
                 Owned: 3 copies in 3 lots
                   x1  Box            2025-03-14  paid $5.10  (Mana Pool)
                   x1  Raktres, Lord of Discounts
                   x1  Marchesa Thatcher
                 Mainboard in: Raktres · Marchesa · Vaevictis's   ⚠ 3 decks, 3 copies
                 Shortlisted in: Prosper's Payday Loans (idea)    ⚠ demand 4 > owned 3
                 History: added to Marchesa 2026-10-03
```

Matched by `oracle_id` so every printing rolls up into one answer.

### Tests

- Contention math with a card in 2 mainboards + 1 idea and 1 copy owned → 2.
- Basics excluded (9 decks run `Swamp`; it must never appear).
- Buy-list total sums only unowned ideas, de-duplicated across decks.
- Card Lookup rolls two printings of one card into a single result.

---

### Phase 3 as built

Where the build differs from, or adds to, the spec above. Read this before
starting Phase 4.

**Contention counts mainboard demand too, which is why it needs no open
shortlist row at all.** `plan_availability()`/`plan_conflicts()` (Phase 2)
only ever see `deck_changes`, so a card that's simply over-subscribed
across mainboards — `War Room` in 4 decks on 3 owned copies, with zero
idea/planned activity — was invisible to every existing check.
`queries.contention_table()` is the first Workbench function that reads
`deck_cards` directly. Run against the live database it found 43 shortfall
rows (close to the plan's observed 39; the gap is normal drift since that
snapshot), with `War Room` (3 owned/4 mainboards) and `Duelist's Heritage`
(2 owned/3 mainboards) landing exactly as predicted.

**An unlinked shortlist row still contends by name.** A backfilled swap
with no `add_scryfall_id` (typed name only) resolves through
`oracle_ids_for_card()`, the same fallback the Phase 2 allocator uses —
`_resolve_change_oracle()` is the one new helper both `contention_table()`
and `card_lookup()` share for it. Mutation-checked via
`test_contention_unlinked_idea_still_resolves_by_name`.

**The Workbench no longer hard-stops on an empty shortlist queue.** The
page used to `st.stop()` the instant `open_df` was empty, on the
assumption that an empty queue meant nothing to show — true before this
phase, false now that contention can fire from mainboard overlap alone.
It now only stops when the queue, contention, AND unlocated lots are *all*
empty; an empty queue with live contention renders the Conflicts/Planned/
Flagged sections as their own "nothing here" states and still shows
Contention and Buy list underneath.

**Buy list is literally two sources concatenated, not a third
allocation pass.** Per the spec, "ideas with zero owned copies" uses a
simple ownership check (`_lots_for_card` sums to zero), deliberately
*not* `plan_availability()`'s claimed/unclaimed accounting — an idea
competing for a copy someone else's plan already claimed is a
`claimed_by`/`in_other_deck` verdict there, not a purchase, and buying it
anyway would be a different decision than this list is for. The
contention half just restates each shortfall row at its cheapest known
printing price. Export is plain text (`moxfield_export.export_buy_list_text`),
matching `export_deck_text()`'s existing convention rather than inventing
a CSV shape nobody asked for.

**Contention's "drop from deck N" acts on the real mainboard, not a
shortlist row.** Spec called for an action, not just a readout, so
`contention_table()` returns `mainboard_deck_ids` ({deck_id: name}) and
the page resolves the deck's actual printing via the new
`queries.deck_card_scryfall_id_for_oracle()` before calling the existing
`writes.remove_deck_card()` — which already logs the removal to that
deck's history (Phase 1), so no new write path was needed. "Buy another
copy" needed no separate action at all: every contention shortfall already
appears in the Buy list below it, so clicking through is just scrolling
down, not a button.

**Card Lookup's History section reads `deck_changes` directly rather than
waiting for Phase 5.** Phase 1 already makes every applied swap/hand-edit
an immutable row, so "what happened to this card recently" was sitting
there unread. Scoped deliberately small (10 most recent, one card) so it
doesn't preempt Phase 5's real per-deck timeline — this is "did something
happen to this card", not "what's this deck's history".

**Representative printing is picked by highest known price**, not first
by set code — closest proxy to "the one you'd actually look at" without
adding a genuine-vs-proxy or most-recent-printing concept that isn't
tracked anywhere yet.

**Verified how:** `tests/test_contention.py` (11 tests: contention math,
basics excluded, no-shortfall-when-covered, unlinked-name resolution, buy
list dedup/exclusion/contention-rows, text export, Card Lookup rolling two
printings into one result, unknown-name, shortlist/demand); full suite at
130 passing; both new sections driven through `AppTest` against a copy of
the live database — the Workbench's Contention "Drop from deck" button
exercised end to end (removed a real mainboard card, logged the history
row, re-rendered with no exception), the Buy list's download button
present, and Card Lookup's search box exercised both for a single exact
match (Sol Ring, 7 owned) and an ambiguous query producing an 11-option
picker; a render sweep of all ten pages (nine `pages/*.py` plus
`dashboard.py`) against the same live-database copy.

---

## Phase 4 — Reconciliation

**Goal:** surface the physical gaps instead of silently tolerating them.

Three work queues, as Workbench sections:

**1. Unlocated lots (178 on the live DB, ≈$1.57k).** Lots where `location`
is NULL or blank. Shown as an editable table — the same `st.data_editor` +
`bulk_update_collection` pattern the Collection page's Edit mode already uses
([`pages/1_Collection.py`](pages/1_Collection.py)) — with a Location dropdown
from `location_catalog`, sorted by price descending so the expensive unknowns
get boxed first. Also: bulk-assign all checked to one location, since these
likely came in as a batch.

**2. Per-deck list-vs-physical diff.** Both directions, which matters:

- *To pull* — in the mainboard, no lot located to this deck. 80 for
  "A Refined Grizzly" (the whole deck), 4 for Vaevictis's, 4 for Nalia,
  2 for Marchesa.
- *To put away* — a lot located to this deck, not in its mainboard. The
  leftovers of past swaps. Not yet measured; the query falls out of the same
  join.

Each row offers "mark as sleeved here" (set the lot's location) or "it's
elsewhere" (reveals where the copy actually is).

**3. Build status per deck.** `% of non-basic mainboard physically located
in this deck`. "A Refined Grizzly" reads ~4%; the rest are 95-100%. Feeds the
home-tile strip in Phase 5.

Basics are excluded throughout — 9 decks run `Swamp` and nobody sleeves those
one lot per deck.

### Tests (`tests/test_reconcile.py`)

- A deck whose mainboard is fully located reports zero in both directions.
- A lot located to a deck but absent from its mainboard shows under *to put
  away*.
- Basics never appear in either direction.
- Build status is 100% for a fully-sleeved deck, 0% for a paper-only one.

### Phase 4 as built

Where the build differs from, or adds to, the spec above. Read this before
starting Phase 5.

**Two query functions, one write.** `queries.unlocated_lots()` (sorted by
lot value, descending) and `queries.deck_reconciliation()` (both diff
directions plus build status for every deck in one pass, or one deck via
`deck_id=`) feed the Workbench's new Reconcile section. The only new write
is `writes.move_lot()`; bulk location edits reuse `bulk_update_collection()`
as specced.

**Matching is by oracle_id, not printing.** A Sol Ring from a different set
sleeved in the deck satisfies the mainboard's Sol Ring. A card with no
`oracle_id` keys on its lowercased name, the same fallback
`_lots_for_card()` uses. "Located to a deck" is the existing exact
`collection.location == decks.name` convention.

**Build % caps each card at what the mainboard needs.** Three lots of a
card in a deck that runs one can't paper over another card that's missing,
so `located` counts `min(needed, located)` per card. A deck with no
non-basic mainboard cards reports `pct = None` rather than 0% or 100%.
Live database: A Refined Grizzly 4% (80 to pull), Vaevictis's 96% (4 to
pull, 7 to put away), Nalia 96% (4), Marchesa 98% (2 to pull, 1 to put
away), Gisa 100% with 1 to put away, the other seven decks clean. The plan's
80/4/4/2 figures reproduced exactly; the to-put-away side, unmeasured when
this was specced, is 9 cards across 3 decks.

**"Mark as sleeved here" can split a lot.** If the deck needs 1 and the lot
holds 3, moving the whole lot would strand 2 copies in the wrong place, so
`move_lot(conn, id, location, quantity)` moves only `quantity`, leaving the
rest as its own lot at the old location. `price_paid` is split in
proportion, which assumes it's a lot total (the live data suggests so: a
2-copy lot paid $0.69 for a $0.53 card). If that assumption is wrong, the
per-lot prices of split lots will be off. A NULL-quantity lot can't be split
and always moves whole. "Put away" uses the same function, defaulting to
`Box`.

**Candidates are ordered free-first.** The lots offered for "sleeved here"
list storage/unlocated lots before ones currently in *another* deck (marked
"in a deck"), so the default pick never raids a built deck silently.

**"It's elsewhere" is a pointer, not a button.** Every candidate lot is
already listed with its current location, so the only case left is a copy
that isn't in the collection at all, which is a Collection-page edit, not a
reconciliation. The row says so rather than adding a second way to do it.

**Verified how:** `tests/test_reconcile.py` (11 tests: fully-located deck
is zero both ways, to-pull, to-put-away, basics excluded, build 100%/0%,
other printing counts, surplus doesn't mask a shortfall, candidate ordering,
unlocated sort, lot split with prorated price, whole-lot move); the
candidate-ordering test caught a real bug where `deck_reconciliation(deck_id=N)`
couldn't recognise a lot sleeved in a *different* deck; full suite at 141
passing; the Workbench driven through `AppTest` against a copy of the live
database (no exceptions; 178 unlocated lots / $1,656.16 shown; a real "Sleeved
here" and "Put away" click each rendered cleanly and updated the diff). The
unlocated table's bulk-assign and per-row save buttons were **not** driven
(`AppTest` can't edit a `data_editor`); they're thin wrappers over
`bulk_update_collection()`, which has its own tests.

---

---

## Phase 5 — History, deck comparison, home tiles

### Per-deck change history (Decks page)

A timeline from `applied`/`dropped` rows, newest first, with the
game-record-since-last-change line that was impossible before:

```
── Changes ───────────────────────────────
  2026-10-04  + Undead Warchief   − Heirloom Blade
  2026-10-04  + Jet Medallion     − Wayfarer's Bauble
              ── 2 games since, 0-2 ──
  2026-09-13  + Ripples of Undeath − Night's Whisper
```

The games join is by date against `games`/`game_participants` (29 games,
2021-10-16 → 2026-09-13). State the obvious caveat in a caption: a 4-player
pod and a handful of games per deck is a signal, not evidence.

### Deck comparison matrix (Decks page, above the deck picker)

All 12 decks, one row each: bracket · interaction · avg CMC · value · W-L ·
game changers · combos found · planned changes · build status. Sortable; the
point is seeing the shape of the pod, which per-deck pages structurally can't
show.

### Home-page tile status strips

[`card_view.render_deck_landing_grid`](dashboard_lib/card_view.py#L533)
currently renders art + name only. Add one line under each name:

```
  [art]
  Gisa, Zombie Gardener
  9 planned · 18 ideas · 1-2
```

Plus a Workbench summary line above the grid, so pending work is visible from
the landing page instead of three clicks deep.

### Tests

- History orders by `resolved_at` desc and includes both applied and dropped.
- "Games since last change" counts only games dated after that change.
- A deck with no history renders without error (all 12 start empty).

### Phase 5 as built

Where the build differs from, or adds to, the spec above. Read this before
starting Phase 6.

**Two queries, one write-free phase.** `queries.deck_change_history()`
returns a newest-first timeline of `change` and `games` rows;
`queries.deck_comparison()` returns one row per deck and feeds *both* the
matrix and the tile strips, so they can't disagree. No new write path, no
schema change. Both are cached in `loaders.py` and cleared by the deck,
collection, game-tracking and combo invalidators (the comparison reads all
four).

**The games rule had to be chosen, not discovered.** `games.date` has a day
and no time, so a game on the day a change was applied can't be placed
before or after it. A game belongs to the deck version made by the latest
change dated *strictly before* it; same-day games count toward the older
list. Mutation-checked: `>` to `>=` fails two tests. `resolved_at` is
SQLite's UTC `CURRENT_TIMESTAMP`, so a change applied late in the evening
local time can read as the next day — only matters for a game logged on that
borderline day, and documented in the docstring.

**Where the "games" lines go.** The spec's mock-up put the line below a
change group, which reads ambiguously (games before it? after?). Built
unambiguously and labelled: "N games since the latest change" above the
newest group, and "N games between these changes" in the gap between two
applied groups on different dates. Two changes on one day share one period
(no zero-width line splitting them), and a `dropped` row never starts a
period — rejecting a card doesn't change the deck. Games older than the
oldest applied change are omitted: history only starts when logging did, so
there's no list to attribute them to. On the live data every deck has zero
applied rows (nothing has been applied yet), so the section renders its empty
state until the first swap is applied; verified with a seeded applied +
dropped pair on a copy.

**Matrix columns and caveats.** bracket · interaction · avg CMC · value ·
W-L · Win % · Game Changers · combos · planned · ideas · built %. Avg CMC
excludes lands including MDFC land faces (same `type_line NOT LIKE '%Land%'`
as `deck_mana_curve`). *Combos* is NULL, not 0, for a deck never checked
against Spellbook (`deck_combo_sync` has no row) — blank cell, explained in
the column help. The matrix is a collapsed expander at the top of the Decks
page, not above a picker: the picker lives in the sidebar, and a deck-tile
click should still land on the deck rather than a table. Sorting is
Streamlit's own column-header sort.

**Tile strip format.** `9 planned · 18 ideas · 1-2 · 4% built`. Zero parts are
omitted, the record appears once a game is logged, and `% built` only while a
deck is below 100 — a 100% on 8 of 12 tiles would be noise. Live: Grizzly
reads `3 ideas · 4% built`; three decks show 96-98% built (the 4/4/2
cards-to-pull from Phase 4). `card_view.deck_status_strip()` is the pure
formatter; `render_deck_landing_grid(status_df=...)` is backward compatible
with no strip. The home-page Workbench line (planned across N decks · ideas ·
unlocated lots) sits above the grid.

**Fixed in passing:** README still described Reconcile as "a stub, fills in
Phase 4" two phases after it shipped. Corrected.

**Not done, deliberately:** the history shows what `deck_changes` recorded, so
the 44 backfilled swaps and any hand edits made before Phase 1 aren't in it.
An applied row whose remove side was typed text (not a linked card) shows that
text verbatim, e.g. lowercase `heirloom blade`; resolving it by name is a
cosmetic follow-up. No per-game card diff ("which 3 cards changed between
these two games") — the timeline lists changes and the record, and that's the
whole claim it makes.

**Verified how:** `tests/test_deck_history.py` (14 tests: empty history,
ordering incl. dropped, both swap sides, the day rule, other decks' games
ignored, between-changes placement, same-day grouping, dropped-doesn't-split,
comparison counts/record/avg CMC/combos-NULL/build %, strip formatting incl.
NaN); full suite 155 passing; home and Decks pages driven through `AppTest`
against a copy of the live database (matrix has 12 rows with the expected
values — Wulfgar's 3 combos match its Combos panel; strips render on all 12
tiles; seeded history renders applied + struck-through dropped + the "3 games
since" line); a render sweep of all ten pages; and both offline scripts
(`_test_prompt_pass13_offline.py`, `_test_spellbook_offline.py`) still pass.

---

## Phase 6 — Deck lifecycle: brewing and dismantling

**Goal:** the two operations the app currently has no concept of — *building
a deck that doesn't physically exist yet*, and *taking one apart to feed
another*. This is the capstone that makes Phases 0-4 pay off: both flows are
built almost entirely out of machinery those phases already deliver.

Right now a brew is indistinguishable from a broken deck, and dismantling is
a blunt `delete_deck()` that dumps every lot into "Box"
([`writes.py:495`](dashboard_lib/writes.py#L495)) — throwing away the one
piece of information you actually wanted, which is *which cards should go
straight into the next deck*.

### 6a — Brewing a new deck (the "Wilson deck" case)

A brew is a deck whose list exists and whose cards are elsewhere. That's
already in the data: **A Refined Grizzly of Noble Heritage** is a complete
100-card list with 3 lots located to it. Its 85 non-basics break down as:

| Where the copy is | Cards |
|---|---|
| Unlocated (own it, box unknown) | 54 |
| In the box, ready to pull | 10 |
| **Only copy sleeved in another deck** | **17** |
| Already sleeved here | 3 |

And the 17 contested cards cluster hard by donor deck — 8 in Tuvasa, 3 in
Nalia, 3 in Wulfgar, 2 in Marchesa. That clustering *is* the useful finding:
building Grizzly means gutting Tuvasa, and that's a decision to make
knowingly, up front, not discover card by card at the kitchen table.

**Build Plan view** (new tab on the Decks page, shown when a deck is in a
brewing state):

```
🔨 Build Plan — A Refined Grizzly of Noble Heritage        85 non-basics · 4% sleeved

── Sourcing ────────────────────────────────────────────────
  📦 From the box                                   10 cards   [pull sheet]
  ❓ Owned, location unknown                         54 cards   → Reconcile
  🃏 From another deck                               17 cards
       Tuvasa, Prequel to Threevasa     8   ⚠ leaves Tuvasa 8 short
       Nalia, Bring your +1 to the Party 3   ⚠ leaves Nalia 3 short
       Wulfgar, the Pain-Harmonicon      3
       Marchesa Thatcher                 2
       Vilis / Vohar / Vaevictis / Raktres / Prosper's   1 each
  🛒 Not owned                                        0 cards   → Buy list
                                                     ───────
  [🖨️ Printable pull sheet]   [Mark deck as built]
```

Each donor line expands to the cards, and each card offers *take it* (queue
the move) or *leave it* (the brew keeps a placeholder and the card joins the
buy list instead). The **donor-impact warning** is the part that doesn't
exist anywhere today: pulling 8 cards out of Tuvasa silently breaks Tuvasa,
and the plan should say so before you sleeve anything.

**Printable pull sheet** — grouped by source location, not by card type,
because you work the box in location order. Reuses the HTML/CSS approach in
[`deck_printout.py:466`](dashboard_lib/deck_printout.py#L466)
(`build_deck_printout_html`), as a sibling function `build_pull_sheet_html`
rather than a flag on the existing one: it's a different document with a
different ordering, and overloading the printout builder would tangle both.

**Brewing state.** Build completion is *derived* (`% of non-basic mainboard
physically located in this deck`, from Phase 4) — derived beats stored
because it self-corrects as you sleeve cards. But intent isn't derivable: a
deck at 4% could be a brew or a built deck whose locations were never
recorded. So one nullable column, additive:

```sql
ALTER TABLE decks ADD COLUMN build_state TEXT;   -- NULL | 'brewing' | 'dismantled'
```

NULL means "a normal built deck" so all 12 existing decks need no backfill.

> **Schema history worth knowing:** Phase 1 of this project removed
> `decks.is_active`/`successor_deck_id` on the grounds that every tracked
> deck is active (`PROJECT_STATE.md:10`, `schema.sql:99`). `build_state` is
> deliberately *not* a revival of that: it's a transient build-workflow flag
> you clear by clicking "Mark as built", not a permanent active/retired
> axis. Keep it that way, or it'll re-accumulate the same dead state.

### 6b — Dismantling / converting a deck (the "Vilis" case)

**Vilis, Blood ATM** has 74 lots sleeved in it, ≈$431. Its non-basic overlap
with every other deck:

| Target deck | Shared non-basics |
|---|---|
| Gisa, Zombie Gardener | 9 |
| Raktres, Lord of Discounts | 5 |
| Vohar / Nalia | 3 each |
| Vaevictis's | 2 |
| Wulfgar / Prosper's / Miara / Marchesa / Grizzly | 1 each |

**Dismantle flow** (Deck Editor → new "Dismantle" section, or Workbench):

```
Dismantle — Vilis, Blood ATM                     74 cards sleeved · $431

Feed into:  [ Gisa, Zombie Gardener  ▾ ]   or   [ + new deck ]

── Direct transfer (9) ──── in both lists; move card-to-card, skip the box
     Feed the Swarm · Malakir Rebirth · Witch's Cottage · ...        [all] [none]
── Return to the box (65) ─ in Vilis only
     ... (grouped by type)                           → location: [ Box ▾ ]
── Target still needs (N) ─ in Gisa's list, not in Vilis
     → resolved from the box / other decks / buy list

  [Preview as planned changes]   →   [Apply]
```

**This needs no new tables.** A dismantle is a batch of `deck_changes` rows —
removals against Vilis, adds against the target — which means:

- The Phase 2 allocator already prevents it from double-promising a card.
- It stages as `planned` and previews before anything is written, like every
  other change.
- It lands in Phase 5's history on both decks automatically: *"Vilis
  dismantled 2026-10-04 → 9 cards to Gisa, 65 to Box."*

That falls out of the one-lifecycle-table decision from Phase 0, and it's the
clearest argument for having made it.

`delete_deck()` keeps its current behaviour for a deck that's genuinely gone.
Dismantling is the *other* path — the deck ends at `build_state='dismantled'`
with its list and history intact, so you can see what it was and rebuild it
later. Deleting destroys the record; dismantling keeps it.

### Tests (`tests/test_deck_lifecycle.py`)

- Sourcing buckets are mutually exclusive and sum to the non-basic count
  (regression against a card appearing as both "in box" and "in deck X" when
  two lots exist).
- Donor impact: taking a card whose only copy is in deck X reports X as one
  short.
- Dismantle with a target produces removals on the source and adds on the
  target, all `planned`, nothing applied until confirmed.
- Direct-transfer rows move the lot's location source → target without a
  stop at "Box".
- A dismantle that would claim one copy twice is caught by the allocator.
- `build_state` defaults to NULL for existing decks and doesn't affect any
  existing query.

---

### Phase 6 as built

Where the build differs from, or adds to, the spec above.

**Two columns, not one.** `decks.build_state` as specced, plus
`deck_changes.dest_location` — which the spec didn't anticipate and the
"no stop at Box" requirement forces. A dismantle's removal has to say
*where the card goes*, or a direct transfer can only be expressed as
"remove to Box, then add from Box", which is exactly the stop the spec
says to skip. NULL keeps the old behaviour (back to `DEFAULT_LOCATION`), so
every pre-Phase-6 row reads as it always did.

**The column pass had to move.** `ensure_schema()` applied column upgrades
*before* creating new tables, and `deck_changes.dest_location` is a column
on a table that is itself an upgrade — on a database older than Phase 0 the
ALTER would have hit "no such table". Tables now come first. No seed
function reads a column added by the other pass, so the order is free;
checked before swapping it.

**Three printings per transfer row, not one.** The lot sleeved in a deck,
the source deck's mainboard printing, and the target's can all be different
printings of one card. The first version used the lot's printing for both
sides, which fails two ways: the removal raises "no longer in this deck's
mainboard", and the add puts a printing on the target that it never chose.
`deck_dismantle_plan()` now carries `scryfall_id` (the lot, for display),
`source_scryfall_id` and `target_scryfall_id`, and each staged row names
its own deck's. Covered by a test with three distinct printings.

**Strays are not deck changes.** Lots sleeved in a deck whose card is *not*
on its list — leftovers of past swaps — have no list change to record, only
a physical one. Staging them as `deck_changes` rows would mean removal rows
that cut nothing, which `apply_change()` rightly refuses. They are reported
as their own `strays` bucket and moved by `writes.move_dismantle_strays()`
beside the staged batch, so the planned rows stay a faithful description of
what happens to the *deck*. (Phase 4's Reconcile "Put away" is the same
concern outside a dismantle.) Live: Vilis has zero strays, so this is
machinery for a case the data doesn't currently exhibit.

**`apply_change()` grew two more shapes.** It was swap-only; a dismantle
needs pure removals and pure adds, and those rows land in the Workbench's
planned queue where the existing Apply button runs them. So it now
dispatches on which sides the row has, and returns `execute_swap()`'s shape
in all three cases. Two guards kept the old behaviour intact: a row with an
`add_name` but no linked printing is still an **error**, not a pure removal
(otherwise an unresolved swap would quietly cut a card and add nothing),
and `apply_changes()` no longer hands a `None` name to `resolve_add`. The
previously unreachable "This swap has no card to replace" error is now the
pure-addition path; no test depended on it.

**A removal-side duplicate guard was missing.** `_open_change_for_card()`
guarded the add side only. Staging the same deck twice — to feed two
different decks — queued two cuts of one card, and applying both would try
to remove a card the first cut already took off the list.
`_open_removal_for_card()` is its twin, and the second staging now skips
the card with a reason rather than double-promising it. Found by a test
written for the spec's "caught by the allocator" item, which turned out to
be caught *earlier* than the allocator — the better place.

**Overlap is not demand, and the spec's table is overlap.** The spec reads
"Vilis ∩ Gisa = 9 shared non-basics" as nine transfers. On the live data
Gisa is fully sleeved, so it *needs* none of them and the correct answer is
zero transfers: a target already holding its copy absorbs nothing. Verified
against every deck — Vilis's only real transfer today is one `War Room` into
the Grizzly brew, and that one was driven end to end (lot moved
Vilis → Grizzly in one step, never "Box"). The Dismantle panel lists each
candidate's *need*, not its overlap, and the Editor shows "target would
still need" so feeding a deck ends with an honest list.

**Sourcing numbers, live.** Grizzly: 83 non-basics (the spec said 85),
3 sleeved · 13 box · 54 unlocated · 13 from another deck · 0 unowned —
summing to 83 by construction. The spec's 10/17 box/other-deck split is the
double-count the allocation order exists to prevent: a card in the box
*and* in another deck is sourced from the box, so copies move out of the
contested bucket. Donors: Tuvasa 6, Wulfgar 3, Marchesa 2, Nalia 2, each
reported with the shortfall it would cause. The spec's per-donor figures
(8/3/3/2) differ for the same reason.

**UI placement.** The Build Plan is an **expander on the Decks page**, not
a tab: that page's top-level sections already are expanders ("Compare all
decks", "Export to Moxfield", "Printable deck sheet") and a lone tab beside
them would be the odd one out. Expanded by default for a `brewing` deck,
collapsed otherwise — a built deck still gets the panel, since "where are
the 4 cards I'm missing" is a fair question for any deck. Dismantle sits in
the **Deck Editor's Deck Info tab, directly above Delete**, because the two
are the same decision made differently; the Workbench is framed as the
global queue, not a per-deck operation. The comparison matrix gained a
`State` column and the home tiles lead their strip with
`🔨 brewing` / `🔧 dismantled`, since a 4% build reads very differently on a
brew than on a deck you thought was finished.

**Not done, deliberately:** no proxy concept. The spec flags a brew's
sourcing plan as where "proxy it rather than gut Tuvasa" becomes the
obvious third option, and the open question still says a decision was
needed by Phase 3; none was recorded, so this phase keeps the binary
take-it / buy-it and the pull sheet's "Not owned" note mentions proxying as
a way to finish the deck. The `location_catalog` `3`/`4` naming question is
likewise untouched. Neither blocks anything here.

**Also not done:** no bulk "dismantle and immediately apply" button —
staging then applying on the Workbench is two deliberate steps, which is
the whole preview-before-apply guarantee. And the per-card *take it / leave
it* checkboxes the spec sketches for each donor line are supported by the
API (`stage_dismantle(transfer_oracle_ids=..., box_oracle_ids=...)`) but
not wired to per-row widgets: `AppTest` can't drive `data_editor` checkbox
state, so a UI built on them would be untestable here, the same limitation
Phases 1 and 4 hit. The panel stages whole buckets; the selection API is
tested directly.

**Fixed in passing:** the README still called print-to-PDF "the remaining
next phase" in two places, two phases after the deck sheet shipped.
Corrected.

**Verified how:** `tests/test_deck_lifecycle.py` (35 tests: build_state
defaulting to NULL and staying invisible to Reconcile/history/comparison,
bucket exclusivity and summing, the box-before-donor priority, donor
shortfall including the leftover-lot and already-short cases, a card
splitting across buckets, build % agreeing with `deck_reconciliation`,
transfer/box/stray splitting, target-already-holding, three-printing
resolution, staging writing nothing physical, removal-before-add ordering,
the lot landing in the target without a Box stop, both decks' history, the
duplicate guards, the allocator conflict, and the two apply-path
regressions); full suite **190 passing**; both offline harnesses
(`_test_prompt_pass13_offline.py`, `_test_spellbook_offline.py`) still
pass; a render sweep of all ten pages; and the real controls driven through
`AppTest` against a copy of the live database — "Mark as brewing" →
`brewing`, "Mark deck as built" → NULL, and "Stage as planned changes"
staging 74 removals + 1 add, flipping Vilis to `dismantled`, while leaving
all 74 lots and both mainboards untouched until applied.

---

## Conventions to hold to

From the existing codebase and project rules — worth restating because this
rework touches the schema, which is where the rules bite:

- **Additive schema only.** New tables/columns via `_SCHEMA_UPGRADES` /
  `_SCHEMA_TABLE_UPGRADES` in `queries.py`, applied on connect. Never drop a
  column, never re-migrate. `migrate.py` stays retired as a re-runnable tool.
- **`maybeboard` and `deck_swap_queue` survive untouched** as a frozen
  backup, even once nothing reads them.
- **Module header comments + inline comments for non-obvious logic**; a
  README section per new feature area, matching how Deck Swap Manager and
  Commander Spellbook are documented in README.md.
- **`queries.py` reads, `writes.py` writes, `loaders.py` caches, pages
  render.** Anything needing live state (the allocator, inventory) bypasses
  the `@st.cache_data` wrappers deliberately, as `card_inventory_status` does
  today — and says so in its docstring.
- **Tests in `tests/`** against the in-memory `conftest.py` fixture, not new
  `scripts/_test_*_offline.py` files — those predate the pytest harness.
- **Streamlit pages are executable here** and should be smoke-tested with
  `AppTest` before being called done.
- Update `PROJECT_STATE.md` and `README.md` at the end of each phase, not in
  one lump at the end.

## Open questions

Both of these outlasted the rework — flagged at the start, still open at the
end, neither blocking:

- **Proxies.** *(still undecided after Phase 6.)* `PROJECT_STATE.md` notes
  the Proxy-in-Location convention was never confirmed, and the Decks page
  deliberately hides proxy flags. The contention table (Phase 3) is exactly
  where "buy, or proxy it" gets decided, and a brew's sourcing plan
  (Phase 6) is where "proxy it rather than gut Tuvasa" becomes the obvious
  third option — so both phases may want a real `proxy` concept. It needed
  a decision by Phase 3 and never got one; both phases shipped with the
  binary buy/take, and Phase 6's pull sheet only *mentions* proxying as a
  way to finish a deck. This is the first thing to settle next.
- **`location_catalog` naming.** *(still open; Phase 4 passed without it.)*
  It currently holds `Box`, `Lands Box`,
  `Lands`, `3`, `4`. If `3`/`4` are box numbers, Phase 4's bulk-assign
  dropdown is the natural moment to normalise them (e.g. `Box 3`) — a
  cosmetic rename of catalog entries, which per the schema comment doesn't
  touch existing collection rows and so would need a deliberate backfill if
  you want the existing 8 lots renamed too.

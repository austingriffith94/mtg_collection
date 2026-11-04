# Tag Suggestions Plan — AI-proposed tags, applied only by you

Working plan from the discussion of 2026-11-01. **The tag feature is built
(Phases 0–7).** See "Already built" for the earlier Advisor changes it relies on. Goal: the Advisor's model reads the oracle text of a deck's mainboard cards
and proposes tags, in one or more tag layers. The proposals appear in a review
panel. A tag is written only when you accept it and press Apply.

Each phase ships on its own: the app runs, tests pass, and nothing is
half-converted at a phase boundary. Go in order.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done

- [x] **Phase 0** — Tidy the existing labels (done 2026-11-01, by conversation:
  stale tags cleared and now dropped when a card leaves a deck, typo and
  Wipe / Lord merges, 19 dead labels deleted, Removal / Interaction re-sorted
  deck by deck; 731 tags remain).
- [x] **Phase 1a** — Tag order: primary and secondary tags (done 2026-11-01).
- [x] **Phase 1b** — Layer vocabularies and definitions (the model's allowed
  list), and the one-time copy into `function` (done 2026-11-01; 343 tags
  copied to the live database).
- [x] **Phase 2** — Proposal engine: prompt, schema, guard, no database writes
  (done 2026-11-01; offline only, no real model call yet).
- [x] **Phase 3** — Backtest against your existing tags, before any UI (done
  2026-11-01: Gemini run on three decks; the Nalia changeling / Mutavault check
  waits for Phase 6, when Nalia gets a vocabulary).
- [x] **Phase 4** — Review panel in the Deck Editor, with Apply (done 2026-11-01;
  checked with a scripted model, not yet with a real Gemini call).
- [x] **Phase 5** — Show a chosen layer on the deck board and in counts (done
  2026-11-03).
- [x] **Phase 6** — Two-pass mode: propose a vocabulary for an untagged deck (done
  2026-11-03; checked with a scripted model, not yet with a real Gemini call or on Ayara).
- [x] **Phase 7** — Docs and wrap-up (done 2026-11-03).

## Decisions

Agreed 2026-11-01:

- **Mainboard cards only.** Tags stay deck-scoped (`card_tags` is keyed by
  deck + printing). Owned cards outside a deck are never tagged and there is
  no collection-wide tag table.
- **Layers are tag types.** `tags.tag_type` already is the layer: the Card Tags
  tab edits one type at a time, and `set_card_tags` replaces only the chosen
  type. Having several layers needs no schema change; tag order (Phase 1a) and
  the vocabulary table (Phase 1b) are the only additions.
- **The AI never writes tags.** It produces proposals only. The only code that
  writes is the Apply button's handler, and it writes only the rows you ticked.
- **Unaccepted AI output never reaches the database.** Proposals live in
  session state only. There is no proposals table, so a page refresh loses
  them. Closing the panel does not: they stay in session state, keyed by deck
  and layer, so reopening shows them without paying for another request.
- **Commanders are tagged** like any other mainboard card. Basic lands are
  skipped, the same as `untagged_deck_cards`.
- **Creature types go through the model, not a calculation.** `type_line` alone
  would miss changelings and cards like Mutavault, a land whose oracle text
  makes it every creature type. Reading type line and oracle text together
  handles these, and it avoids a second code path.
- **The panel is a pop-up:** `st.dialog(width="large", dismissible=False)`, up
  to 1280px wide, with explicit Apply, Discard and Close buttons. The app
  already uses dialogs for this kind of task. Turning off dismissal means a
  stray click outside or ESC can't close it mid-review.
- **Existing functional tags are copied into `function`,** with `strategy` left
  as it is. Removing them from `strategy` is a separate, later decision.
- **Removal and Interaction are defined as follows.** **Removal** takes things
  off the board. **Interaction** is the broader group of everything else that
  affects an opponent's plays or board without doing that, **counterspells
  included** (changed 2026-11-01: a counter stops a plan more than it removes a
  target, so it sits between removal and protection).
  A card gets one or the other, not both, so Removal + Interaction together is
  the total interaction in a deck. The definitions in the vocabulary carry this
  wording, because the model follows definitions and ignores intent. Edge cases
  are written into them and can be changed in the editor: bounce is Removal (it
  takes the thing off the board); tapping down, goad or forced combat, stealing,
  taxing and damage pingers are Interaction.
- **Tags stay focused.** A deck's active vocabulary in a layer is capped (about
  10 labels), a tag should cover enough cards to be worth having (at least 3),
  and the model is told to tag a card only where it clearly does the thing and
  to prefer one tag, and two at most. The review panel reports tags that would
  stay thin or grow too broad before you apply anything. See "Keeping tags
  focused" below.
- **Gemini is the default model for suggestions,** the cheaper option. Claude
  has no API key set up yet, so it is untested and not expected to be used. The
  backtest can compare it later if a key appears.
- **The model drafts definitions for your existing `strategy` labels,** from the
  cards that carry each label. You edit them before saving, and a label with no
  saved definition is not offered to the model. This reuses the review panel
  (Phase 4), so the drafts follow the same rule as tags: nothing is saved until
  you accept it.
- **Each card has one primary tag per layer; the rest are secondary.** A card
  sits in one group only, under its primary, and that group's header shows
  secondary cards as "(+X)". Any tag count counts both. See "Primary and
  secondary tags" below.

Agreed after the backtest (2026-11-01):

- **`strategy` stays as it is.** Existing data and the deck board depend on it
  (Nalia's creature-type tags live there). The new layer is called `function`
  (ramp, removal, interaction, aggro, ...). More layers can be added by name.
- **Additive Apply by default.** Accepting a proposal adds labels to the card
  and keeps the ones it has. Removing or swapping a label is a separate
  per-row choice, shown as such.
- **At most 2 tags per card per layer** (was 3; the backtest showed the second
  tag is usually noise). A card with nothing fitting gets no tag.
- **The Advisor package stays read-only.** `dashboard_lib/advisor/` returns
  proposals; the page calls `writes.*` after you click. This keeps the claim in
  `advisor/README.md` ("nothing here writes to the database") true.
- **Rows that only add a secondary tag start unticked** in the review panel, as
  do unverified, `conflict` and thin-label rows. Only a verified row that gives
  a card its first tag, or a new primary, starts ticked.
- **Removal and Interaction are exclusive in the guard.** A row with both is a
  violation, the primary is kept, and the other is dropped. The pairs are
  `EXCLUSIVE_LABELS` in `context.py`.
- **Large decks go out in batches of 50 cards** (even split), each with its own
  retry. A batch that fails keeps the finished ones.
- **Errors are specific and logged.** Phase 4 shows the failure as a retryable
  message, with the batch, the cards it covered and the provider's reason, and
  keeps any proposals already on screen. `tagging` logs each failed batch and
  retry at WARNING.

## What the database holds today

Read-only snapshot, 2026-11-01 (it moves as you tag):

- 761 tag assignments, all of type `strategy`, using 52 labels (the `tags`
  table holds 71 rows; 19 are unused). 11 of 12 decks are mostly tagged; Ayara
  has none.
- 50 of the 761 rows are stale: the card is no longer on that deck's list.
  Every count in this section includes them.
- Each deck uses 5–10 labels, and every tagged card has exactly one tag.
- 335 of the 761 (44%) are already functional labels sitting in `strategy`:
  Card Adv 114, Interaction 77, Ramp 57, Removal 42, Protection 27, Wipe 12,
  Board Wipe 5, plus the typo Interacton 1. Nalia has none of them; its labels
  are Cleric, Warrior, Wizard, Rogue and Any.
- Of 46 cards tagged in two or more decks, 28 carry different labels. Some
  are real (Mayhem Devil as Pinger vs Interaction), some are drift (Chaos
  Warp as Removal vs Interaction).
- 40 of the 52 labels are used in one deck only. Near-duplicates exist:
  Wipe / Board Wipe, Lord / Lord Effect, Interaction / Removal.
- Oracle text is stored for all 1,263 distinct owned cards (about 164
  characters on average). Every deck has a description for the prompt.

## The design

### Layers and vocabularies

A layer has a **vocabulary**: the labels the model may use, each with a
one-line definition. Without definitions, labels like Target, Utility and Any
mean nothing to a model. The definitions go into the prompt and the response
schema restricts the model to the labels.

- `function` has a **menu** of strategy ideas, each with a definition, using your
  existing wording where you have it: for example Card Adv, Ramp, Removal,
  Interaction, Wipe, Protection, Tutor, Recursion, Token Maker, Sac Outlet,
  Fodder, Pressure, Voltron, Tax/Stax, Politics, Combo Piece, Win Con, Fixing.
  The menu can be broad because a deck uses only part of it: each deck
  **activates** a focused subset, and only the active labels go to the model.
  Menu entries and definitions are editable.
- `strategy` takes its vocabulary from the labels already on that deck's
  cards. A deck like Nalia, tagged by creature type, keeps working because the
  model only chooses among labels you already use.
- A layer with no vocabulary yet is what Phase 6 handles.

Storage: a small new table `deck_tag_vocab(deck_id, tag_type, label,
definition, PRIMARY KEY (deck_id, tag_type, label))`, added with the same
idempotent pattern as the other catalog tables in `queries.py`. It also has to
join the deck-scoped tables `delete_deck` clears.

`deck_tag_vocab` holds a deck's **active** labels; the menu itself stays a
Python constant. A deck's vocabulary is computed on read: the labels in this
table plus the labels on its cards right now. The board and the Workbench let you type new
labels freely, so a stored copy would go stale, and opening a panel should not
write to the database.

### Proposal engine (`dashboard_lib/advisor/tagging.py`)

One request per deck and layer, modelled on `analysis.run_analysis`:

1. **Prompt:** deck name, commander, stated plan (`decks.description`), the
   layer's vocabulary with definitions, and every mainboard card with type
   line, mana cost, oracle text, and its current tags in this layer. A few of
   the deck's already-tagged cards act as examples of how you use each label.
2. **Schema:** card names are an enum of the deck's mainboard; tags are an
   enum of the layer's vocabulary; `tags` has `maxItems` 2. Per card:
   `tags`, `primary`, `evidence_quote`, `reason` (one sentence).
3. **Guard** (`guard.py`, same violation-list shape as `check_analysis`):
   - card is a mainboard card; tag is in the vocabulary; at most 2 tags, and not both Removal and Interaction; the
     `primary` is one of the card's own tags (and absent when `tags` is empty);
   - every card sent came back, so a skipped card reads as "not reviewed" and
     not as "no tag fits" (an empty `tags` list is the model's way to say that);
   - `evidence_quote` is a verbatim substring of that card's type line or
     oracle text (the existing `_norm` comparison). The type line has to count,
     because a creature type is often only there. For plan-based labels the
     quote is a weak proof, so a failed quote marks the row **unverified**, it
     does not drop it;
4. **One retry** that lists the violations, then unverified rows are returned
   marked, never silently dropped (same rule as analysis mode).
5. **Diff, not a list.** Each result row is classified against the card's
   current tags: `new` (adds a label), `same` (already tagged that way),
   `conflict` (model picks a different label than you have). `same` rows are
   hidden by default; `conflict` rows are the interesting ones.

Creature-type layers (Nalia) need one prompt rule: a card counts as a type if
its type line or its oracle text makes it one, which covers Changeling and "is
every creature type" (Mutavault). A label's definition can narrow this further,
for example a type label meant for creatures only.

### The review panel (Deck Editor → Card Tags tab)

A "Suggest tags" button opens the pop-up:

- Layer picker, model picker (reusing the Advisor's provider choices, starting
  on Gemini), a Suggest button, and a token/cost note.
- A summary line above the table: tags per card on average, and the thin and
  broad labels.
- A table of proposals: Accept checkbox, card, current tags, proposed tags,
  reason and quote, a verified / unverified badge. Verified `new` rows start
  ticked; unverified rows and `conflict` rows start unticked.
- A per-row "replace instead of add" choice for conflicts.
- **Apply** writes only ticked rows, in one transaction, through a new
  `writes.apply_tag_proposals()`. It cannot reuse `set_card_tags` or
  `bulk_set_card_tags`: `set_card_tags` commits per card, so a failure partway
  would leave half a batch. It then shows "Added N tags on M cards" with an
  Undo. The board's Undo only covers one card, so this needs a batch version
  that restores the previous tag sets. A purely additive Apply needs no backup;
  if any ticked row is a replace, it takes a `backup.snapshot` first, as the
  app's other destructive writes do (`delete_deck`, `prune_collection`).
- **Apply re-checks first.** Proposals can outlive the state they were made
  from. Before writing, each row is compared to the deck now: a card no longer
  on the mainboard is skipped, and a card whose tags in this layer changed
  since Suggest is skipped and listed, so a replace can't undo a manual edit.
- The Card Tags tab shows "N proposals waiting" when any exist for this deck,
  so a closed panel isn't forgotten. Ticks are stored with the proposals, so
  they survive Close too.
- Nothing is written until Apply. Close keeps the proposals for this session
  and reopening shows them. Discard clears them. A new Suggest for the same
  deck and layer replaces them, and asks first if any are still unapplied.

### Keeping tags focused

Three guards against noise, all tunable constants (the numbers are starting
points to adjust after the backtest):

- **Active vocabulary cap, about 10 per deck and layer.** The vocabulary editor
  warns past it. A deck with no active `function` labels starts from the menu
  entries its cards already use (from the Phase 1b copy), not from the whole menu.
- **Minimum bucket of 3.** After a proposal run, the panel computes what each
  label's card count would be if every ticked row were applied. A label that
  would end with 1–2 cards is marked **thin**, and its rows start unticked, with
  a button to tick them anyway or to drop that label from the deck's vocabulary.
  The same report marks a label that would cover over about 40% of the deck as
  **broad**, since a tag on nearly everything says nothing.
- **Prompt rules.** Tag a card only when the label is a main thing it does, not a
  side effect. Prefer one tag, two at most. Return an empty list rather than the
  nearest label. The guard can't judge this, so the backtest measures it: it
  reports tags per card and thin or broad labels next to accuracy.

The Phase 6 two-pass mode follows the same limits: pass one proposes at most 10
labels, drawn first from the menu.

### Showing layers elsewhere

The deck board groups by "Strategy tag" and `deck_strategy_tag_counts` reads
`tag_type = 'strategy'` only, so a second layer is invisible there until Phase
5 gives the board a layer selector. This is optional and can wait.

`queries.untagged_deck_cards` is not optional. It counts a tag of any type, so
adding a `function` tag to a card would silently drop it from the Workbench's
untagged list for `strategy`. Phase 1 makes it take a `tag_type`.

### Primary and secondary tags

**Storage.** `card_tags` gains `position INTEGER NOT NULL DEFAULT 0`, added
through `_SCHEMA_UPGRADES`. Within one card, deck and layer, the lowest position
is the primary. Every existing card has one tag per layer, so all of them
become primary with no data change.

**Display (grouping).** A card appears once, under its primary. The board
already does this: `groupByTag` uses `tags[0]` so that a card in two stacks
doesn't double-count, and its only flaw is that `tags[0]` is arbitrary today.
The header then adds secondary cards that are not drawn in that stack:

    Ramp 9 (+3)     9 copies with Ramp as primary, 3 more with it as secondary

**Aggregation (counts).** Everything that counts tags counts primary and
secondary alike, so `deck_strategy_tag_counts` above would say Ramp 12. These
totals overlap, so they can sum past the deck size and must not be added up
into a deck total or a share of the deck.

**Code that has to change for order to be real:**

- `set_card_tags` saves the order it is given. `get_card_tags_by_type` sorts by
  position.
- `deck_cards_dataframe` sorts labels by position. `GROUP_CONCAT(DISTINCT ...)`
  has no ordering, so this needs a pre-sorted subquery.
- `bulk_set_card_tags` and the board's `set_tags` op compare tags as sets, so
  moving a label to the front is treated as "no change" and never saved. Both
  have to compare in order.
- The printing fix (`writes.py`, near the `deck_cards` rewrite) deletes and
  re-inserts a card's tags and would lose position, so it has to carry it.
- The Decks page's "Strategy Tag" grouping uses the whole `strategy_tags`
  string, so "Ramp,Removal" would become its own group. It has to group on the
  primary.

In the Card Tags tab and the board's tag box, the first label typed is the
primary, which needs no new UI.

**Proposals.** The model marks a primary. On Apply, a card with no tags in that
layer takes the model's primary. A card that already has tags keeps its primary
and the new ones go after it, unless you choose replace on that row.

## Phases

### Phase 0 — Tidy the existing labels

Mostly manual. Run the existing stale-tag cleanup in Card Database → Clean up
to clear the 50 stale rows. Fix the Interacton typo. Merge Wipe / Board Wipe and Lord / Lord Effect. The
Removal / Interaction line is decided (see Decisions), so the work here is
moving the 119 existing Removal and Interaction tags onto it. The model learns
your inconsistencies, and the backtest measures against them, so this is worth
doing before the backtest. Once Phase 4 exists, its conflict rows (where the
model disagrees with your current label) are a quicker way to find the cards
that need moving than reading all 119.
Optional helper: a "merge label A into B" write that repoints `card_tags` rows,
since renaming a `tags` row changes it in every deck.

### Phase 1a — Tag order

`card_tags.position` and every ordering change listed under "Primary and
secondary tags": `set_card_tags`, `get_card_tags_by_type`,
`deck_cards_dataframe`, the order-aware comparisons, the printing fix, and the
Decks page grouping. This is its own phase because it touches the board, the
Decks page and existing writes, and it is useful even if the AI part is never
built. Tests against the offline database, plus a browser check of the board.

**As built:** `writes.same_tags()` is the one order-aware comparison, used by
`bulk_set_card_tags` and the board's `set_tags`. One change the list above
missed: `get_all_card_tags` also sorts by position, or a board Undo of a removal
would lose the order. The printing fix keeps each tag's earliest position when
two printings merge. Ties on position sort by label. The Decks page groups on a
derived `primary_tag` column; its filter and table still use `strategy_tags`.
Headless Edge check on a database copy: a two-tag card stacks only under its
primary, and reordering in the tag box saves and moves it.

### Phase 1b — Vocabularies and definitions

- `deck_tag_vocab` table and `writes` helpers (list / set / delete a label),
  including the `delete_deck` cleanup.
- The `function` menu with definitions as a Python constant, and per-deck active
  labels in `deck_tag_vocab`.
- `untagged_deck_cards` takes a `tag_type`, and the Workbench passes it.
- Storage and prompt support for definitions of the labels you already use in
  `strategy`. Many are specific to one deck ("Rakdos Target", "Gisa Help",
  "Any"), and the model can't guess what they mean. The drafting itself waits
  for the Phase 4 panel, so until then only the `function` layer, whose defaults
  are written in the constant, can be suggested for.
- **One-time copy into `function`,** after the Phase 0 cleanup. For each card in
  each deck tagged Card Adv, Interaction, Ramp, Removal, Protection or Wipe, add
  the same label in `function` as its primary. `strategy` rows are not touched.
  It takes a `backup.snapshot` first, shows the count before running, and is
  safe to re-run. Expect fewer than 335 rows once stale tags and duplicates are
  gone.
- Tests against the offline database.

The vocabulary editor waits for Phase 4, so no UI is built before the backtest
shows the idea works. Definitions can be edited in the constant until then.

**As built:** the menu and the list of copied labels are in
`dashboard_lib/tag_vocab.py`. Reads are in `queries.py`, not `writes.py`, so the
Advisor can use them without importing the write layer: `active_tag_vocab`, and
`deck_tag_vocabulary`, whose `defined_only=True` is the list Phase 2 sends to the
model. `writes.py` has `set_tag_vocab_label` and `delete_tag_vocab_label`.
The Workbench's untagged list and its section count follow the Tag type picker,
which now stays visible when the list is empty.

The copy is `scripts/copy_function_tags.py`, which previews by default and copies
with `--apply`. It was run on the live database on 2026-11-01: 343 tags (Card Adv
110, Removal 92, Ramp 59, Protection 36, Interaction 27, Wipe 19), backup
`backups/mtg_collection_manual_2026-11-01_172144.db`. The "fewer than 335"
estimate was out of date, since labels had grown after that snapshot. Every
count matched the live `strategy` labels. A later `strategy` tag is not mirrored
automatically; re-running the script picks up new ones.

### Phase 2 — Proposal engine

- `advisor/tagging.py` and a `skills/tag_suggester.md` system prompt.
- Schema in `schemas.py`, checks in `guard.py`, diff classification.
- `FakeProvider` tests: out-of-vocabulary tag, unknown card, too many tags,
  failed quote, the retry path, `same` / `new` / `conflict`.
- No page changes. Move `make_provider` out of `pages/10_Advisor.py` into the
  advisor package so the Deck Editor can use it too.
- Let a caller set the output limit. `AnthropicProvider` hardcodes `max_tokens`
  at 4096, and a full deck's answer will likely exceed that (see Risks). A cut
  off answer fails to parse, so the request would fail outright. Gemini sets no
  limit of its own, so it isn't affected, but its hidden thinking tokens count
  toward output and the first run should check that. The Claude change can be
  tested offline only until a key exists.
- The thin / broad report (see "Keeping tags focused") is computed here, in
  code, from the proposals and the deck's current tags.

**As built:** `tagging.run_tag_proposals(provider, ctx, hide_current, examples)`
with `context.build_tag_context`, `schemas.tag_schema`, `guard.check_tags` and
`skills/tag_suggester.md`. `hide_current` and held-out `examples` are what the
backtest needs. `make_provider` and `PROVIDER_CHOICES` moved to `providers.py`.
`get_card_tags_by_type` moved to `queries.py`, so the Advisor doesn't import
`writes`; `writes` keeps the name.

Changes from the design above:

- **The retry asks only for the cards with a problem,** and its rows are merged
  over the first answer. Failed quotes will be common, and a full second answer
  would pay for the whole deck again and could lose good rows.
- **`chat` takes `max_tokens` and `timeout` per call.** The timeout is new: the
  answer arrives all at once, so a long one could pass the 120s default.
  Tagging uses 16,000 and 300s. Gemini ignores `max_tokens`, because hidden
  thinking tokens would count against it.
- **There is a fourth status, `none`,** for a row that proposes nothing. `same`
  also covers a proposal that is a subset of the card's tags. `new` covers one
  that adds a label and keeps at least one existing label; `conflict` shares none.
- **A row's `tags` hold only legal labels, primary first.** A rejected label stays
  in the row's violations and the raw `data`, so Apply can't write it. An
  unknown or repeated card keeps its row with no tags.
- **Several printings of a card are one entry,** carrying all their tags and
  `scryfall_ids`. No deck has this today; Apply will need to write to each.
- **`focus_report` counts copies,** so a 30-copy card is not thin, and only
  reports vocabulary labels. `TagResult.focus` assumes the verified `new` rows
  are applied; Phase 4 recomputes it from the ticked rows.

Found by a read-only dry run over the live decks:

- **The role warning was dropped.** It fired on 144 of your 280 existing tags
  that map to a role: Wipe 15 of 19, Removal 55 of 92, Card Adv 61 of 110, Ramp
  13 of 59. These were misses in the regexes (fight, "each creature", impulse
  draw, and lands are never `ramp`), not bad tags. The guard keeps its other
  checks, and the backtest will show a contradicted tag better than a regex.
- **Input is smaller than the Risks section guessed:** about 3,500–6,100 tokens
  of prompt plus about 700 of schema (estimated at 4 characters per token). The
  real numbers come from the first run.
- **Decks don't all have six `function` labels.** They have 2–6, and Nalia and
  Ayara have none, so a run on either stops with an error before any call.

### Phase 3 — Backtest

A script in `scripts/` (run by hand, uses the real API): for one tagged deck,
have the model re-tag from the same vocabulary and compare to your tags. Hiding
the tags has to cover the whole prompt: the current-tags column is left out, and
the examples come only from a held-out handful of cards that aren't scored.
Otherwise the model is reading the answer key. Report exact match, match on any
of the model's tags, and the disagreements. Run it on two or three decks with a
spread of styles (Nalia, a generic one, Marchesa). On Nalia, check the
changelings and Mutavault by name. Use the result to tune the prompt and
definitions, and to decide whether the idea is good enough to put a UI on.

Decided 2026-11-01: the first backtest uses only the six labels already on each
deck's cards (Card Adv, Interaction, Ramp, Removal, Protection, Wipe), the cleanest
comparison with your data. Activate more menu labels per deck only if the results
look too narrow.

Nalia has no `function` tags (see the Phase 2 notes), so it can't be scored on
that layer as planned: it needs labels activated and something to compare
against, or a different third deck.

Run it on `function` only at this stage: `strategy` labels have no definitions
until Phase 4, and a `strategy` vocabulary built from the deck's own labels
already contains the answers, so that score would only measure choosing among
known labels. `function`, with its default vocabulary, is the harder and more
honest test. Each run spends money, so the script prints a token estimate and waits
for a go-ahead before the first call. Gemini is the model under test. If a
Claude key is ever added, rerunning the same script on it is the way to find out
whether the quality differs enough to matter.

**Engine changes after the backtest** (committed before Phase 4): max 2 tags,
the exclusive-pair guard, batching of 50 cards, and per-batch error text with
logging. Partial results: `TagResult.error` can be set while `checked` still
holds the finished batches, and the failed batch's cards are in `missing`.

**As built:** `scripts/backtest_tags.py`. Hold-out is the first two tagged cards
by name for each label (by primary), used as the prompt's examples and not
scored. It prints the token estimate (about 5,700–6,100 input for decks 12, 2 and
9) and asks before spending. Scores are exact, any-match and primary-match over
tagged, non-held-out cards; cards you left untagged are counted as extras, not
scored. It also prints tags per card, thin and broad labels, unverified rows, and
every disagreement with the model's reason. `--card NAME` prints one card's full
row. Run: `python scripts/backtest_tags.py --deck 12 --deck 2 --deck 9`.

**Results, 2026-11-01** (gemini-3.5-flash, second run; a card counts once per deck):

| Deck | Scored | Exact | Any | Primary | Extras |
| --- | --- | --- | --- | --- | --- |
| Marchesa (2) | 22 | 17 | 20 | 20 | 12 of 56 |
| Grizzly (12) | 35 | 29 | 34 | 34 | 7 of 36 |
| Vaevictis (9) | 34 | 31 | 34 | 33 | 11 of 44 |
| Total | 91 | 77 (85%) | 88 (97%) | 87 (96%) | 30 of 136 (22%) |

- The first run gave Grizzly 32 exact and Vaevictis 30, so a deck's exact score
  moves a few cards between runs. Any and primary barely moved.
- 11 of the 14 disagreements are the model adding a second tag to your label.
  Your cards mostly carry one tag, so exact match counts these against it. The
  other 3 are Do or Die, Thaumatic Compass and Homeward Path.
- Extras are the tag rows Phase 4 would most often add. Many look like real gaps
  (Anguished Unmaking, Tireless Tracker, the monarch Courts); a few are a stretch
  (Noble Heritage as Protection, Gisa as Interaction).
- The model broke the "Removal or Interaction, never both" rule once, on
  Manglehorn in the first run. The guard doesn't check this.
- Actual use: about 5,300 tokens in and 10,000-13,000 out per deck, near the
  16,000 limit. A retry took Vaevictis to about 16,900 in. Gemini's output count
  may include hidden thinking. No rows were unverified after the guard, and none
  went missing.
- One of six requests failed with a 503 after the provider's own retries, so the
  panel has to show that error cleanly.

Limits: the score is agreement with your tags, and Removal / Interaction were
re-sorted by these same definitions in Phase 0, so those two labels are partly
measured against themselves. Nalia's changelings and Mutavault remain unchecked,
and `strategy` was not tested.

### Phase 4 — Review panel

**Decisions already made for it:** secondary-only rows start unticked and a failed
request shows a specific, retryable error (see Decisions).

The panel above, with Apply and Undo, plus the vocabulary editor in the Card
Tags tab. The panel gets a second kind of proposal, **definitions**: a "Draft
definitions" action sends each undefined label with a sample of the cards that
carry it (name, type line, oracle text) and gets back one line per label, shown
in the editor for you to change and save. Labels used by one card only get a
note that the draft rests on a single example. Tests: `writes.apply_tag_proposals`, Apply touching only ticked rows,
the stale-row skips, no write on Suggest, and an AppTest pass over the panel
(pages can be executed here, per project notes).

**As built:** the pop-up, its Undo and the vocabulary editor are in
`dashboard_lib/tag_panel.py`, called from the Card Tags tab. `writes.py` gained
`apply_tag_proposals` and `undo_tag_proposals`. `tagging.py` gained
`starts_ticked`, `estimate_tokens` and `draft_definitions`, with
`skills/tag_definitions.md`. `focus_report` takes `replaced`, so a ticked replace
row counts as losing its old labels.

Changes from the design above:

- **Replace works on any row, not only conflicts.** The table has one Replace
  column, and the same choice also trims a `same` row down to the proposal.
- **The cost note is tokens only.** The app stores no prices, so it shows
  estimated tokens before a run and actual tokens after. The output estimate
  is 140 tokens per card, from the backtest.
- **Retry covers only the unreviewed cards** when a batch failed or the model
  skipped some, and merges them in, so finished batches are not paid for twice.
  Ticks on existing rows are kept.
- **Thin rows hold their labels.** A label whose rows all start unticked counts
  zero, so it would drop out of the thin report. `Proposals.held` keeps it
  listed with its tick and drop buttons.
- **Undo leaves a later edit alone.** A card whose tags changed after Apply, or
  that left the mainboard, is skipped and named, as Apply does for Suggest.
- **Applied and skipped rows leave the proposals;** unticked rows stay waiting.
  Skipped cards show in a toast and beside the Undo button.
- **Definition drafts have no retry.** A label the model left out is reported and
  can be drafted again. Drafts fill the empty Definition cells, marked as not
  saved, and Save definitions stores what is in the table. A definition
  that matches the built-in default is stored empty, so a later change to the
  default reaches it.

Checks: 892 tests pass, including `tests/test_tag_panel.py`. A headless Edge run
on a database copy with a scripted model used deck 2: ticks went 56, 57, 58, 59,
then 58 on an untick, with none lost, and survived Close and reopen. Apply added
58 tags, taking the copy from 30 to 88 `function` tags on that deck, and tagged
cards kept their primary. The dialog has no close X. The live database was not
touched. No real Gemini call has run through the panel, and definition drafts
have only run against the fake provider.

### Phase 5 — Layer on the board

Layer selector for the board's grouping and for the tag counts, and the "(+X)"
in the stack headers, with a hover listing those cards. Browser check against a
database copy, as for the other board changes. This phase is what would let you
decide later whether to remove the copied labels from `strategy`.

**As built:** the toolbar gets a "Tags" picker (one button per layer the deck has,
`strategy` first), shown only when there are two or more. It regroups, relabels the
hover preview and the tag editor in the browser, and the choice is kept across
reruns like group-by. The grouping option is now just "Tag" (was "Strategy tag"). A
stack header shows `Card Adv 3 (+2)`, and its hover lists the secondary cards. A
label that is only ever secondary has no stack, so its cards appear under their own
primaries. The board's tag editor writes the layer on screen: `set_tags` takes an
optional `tag_type` (default `strategy`), and its Undo carries it. The Decks
page's tag breakdown has a layer picker too, via `queries.deck_tag_counts`;
the counts overlap, and the page says so. New: `queries.deck_tag_layers`,
`tests/test_board_layers.py` (902 tests pass). Headless Edge check: switching
layers regrouped the board and the header showed `(+2)` with its hover list.
The check began on a copy of the database, but the override didn't take and the run
used the live one. It wrote two test tags, which were reverted straight after
(deck 2's `function` counts match what they were).

### Phase 6 — Two-pass mode for an untagged deck

Pass one: the model proposes up to 10 labels with definitions from the
commander, plan and cards. You edit the list in the vocabulary editor. Pass
two is the normal Phase 2 request using that vocabulary. Ayara is the test
case.

**As built:** `tagging.propose_vocabulary(provider, ctx, menu)` is pass one: one
request, no retry, no writes. It sends the deck, its plan, the `function` menu
(none for other layers) and every mainboard card without tags, under
`skills/tag_vocabulary.md`. `schemas.vocabulary_schema` caps the list at
`MAX_PROPOSED_LABELS` (10), and `guard.check_vocabulary` keeps a menu label's
spelling and definition, and drops blank, repeated and surplus labels. In the
Card Tags tab, **Labels and definitions** shows a "Propose a vocabulary" button
while the layer has no defined labels. The result is an editable table (Keep,
Label, Definition) held in session state, and **Save labels** stores the kept
rows through `writes.set_tag_vocab_label` (a definition equal to the menu default
is stored blank, as in the definitions editor). Pass two is the normal Suggest
tags, whose empty-layer message now points to the button. Tests: 907 pass.

### Phase 7 — Docs

`advisor/README.md` (new file, new skill, and that the package stays read-only
while the page does the writing), a README note for the Card Tags tab, and a
line in `PROJECT_STATE.md`.

**As built:** all three, plus README rows for the two new skills.

## Already built (outside the phases above)

Advisor improvements made on 2026-11-01 (committed on this branch ahead of Phase 2) that this plan builds on.
They change analysis mode only and touch no tags:

- The prompt now includes the deck's win conditions, strengths, weaknesses,
  themes and combos.
- Candidate cards are chosen by deck fit and role, plus combo completers, in
  place of the old role-only ranking.
- Suggestions carry warnings for a broken combo, a Game Changer over the
  bracket limit, or a combo added to a bracket 1–2 deck.
- Code is in `dashboard_lib/advisor/context.py`, `guard.py` and `analysis.py`;
  see `advisor/README.md` for the details.

Deliberately not built: feeding rejected ideas back to the model (reinforcement
bias), and hand-written narrow skills.

## Follow-ups that depend on this plan

- **Function count warning.** Once `function` tags exist, `guard.py` can warn on
  a swap that lowers a count ("Ramp 9 → 8"), next to the existing combo and
  Game Changer warnings. It can also replace the regex role counts in the prompt.
- **Deck details drafts.** The review panel can also draft win conditions,
  strengths and weaknesses for a new deck. The grounding is the same as tags:
  the model sees the deck's own cards, and each draft line must name deck cards
  with a quote that the guard checks. You edit it and save; it never writes on
  its own. A draft for a deck with existing notes should be a side-by-side
  diff, because these notes are your own intent and feed the Advisor's prompt.

## Risks

- **Quotes prove the text is real, not the tag is right.** The guard catches
  invented cards, tags and quotes. It cannot tell you that "Pinger" was the
  better label. That is what the panel and the backtest are for.
- **Your labels are the answer key and they are noisy.** The backtest will
  undercount accuracy until Phase 0 is done.
- **Plan-based labels (Fodder, Target, Politics)** depend on the deck's plan,
  which the model only sees as the one-line description. Definitions and
  examples carry most of the weight here.
- **Output size is the tight limit, not input** (answered by batching, above). Estimate for a 90-card deck:
  5–8k tokens out, since every card returns tags, a quote and a reason. Input is
  larger still once definitions and the schema are counted. The first real run
  should record actual numbers. If one request proves too big, split the deck
  into batches and merge the results. Each Suggest spends money, so the panel
  shows the model and token count after each run and never re-runs on its own.

## Open questions

None blocking. The thresholds in "Keeping tags focused" (cap of 10, minimum of 3, broad over
40%) are starting values to adjust after the backtest, not open questions.

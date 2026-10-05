"""
Write layer for in-dashboard editing: card_tags, deck metadata,
deck_cards (mainboard), deck_changes (the idea -> planned -> applied
shortlist), deck_themes, and the collection
(including prune_collection, the automatic cleanup step run as part of
the Card Database page's Card Data "Refresh" action). Plain sqlite3, no
Streamlit dependency, so it can be unit-tested directly against a
scratch copy of mtg_collection.db.

Migration is one-way, CSV -> DB: scripts/migrate.py wipes and rebuilds
the whole database from the CSVs on every run, with no attempt to
preserve anything written here. That's intentional — see migrate.py's
module docstring and the README. Use the CSVs + migrate.py for your
initial import, then manage things here from then on.
"""
import datetime
import logging
import sqlite3

from . import queries as q

logger = logging.getLogger(__name__)

# The fallback storage Location a deck's collection.location rows are
# reassigned to (Prompt Pass 13) instead of being cleared to NULL —
# used by delete_deck() (deleting a deck) and execute_swap() (removing
# a card from a deck via the Swap Manager). "Box" is always offered in
# the Location dropdown regardless of the master catalog's contents
# (see queries._seed_location_catalog / schema.sql's location_catalog
# comment), so a row reassigned here always resolves to a real,
# selectable option.
DEFAULT_LOCATION = "Box"


def get_or_create_tag(conn, tag_type, label):
    tag_type = tag_type.strip()
    label = label.strip()
    conn.execute("INSERT OR IGNORE INTO tags (tag_type, label) VALUES (?,?)", (tag_type, label))
    row = conn.execute(
        "SELECT tag_id FROM tags WHERE tag_type=? AND label=?", (tag_type, label)
    ).fetchone()
    return row[0]


def list_tag_types(conn):
    rows = conn.execute("SELECT DISTINCT tag_type FROM tags ORDER BY tag_type").fetchall()
    return [r[0] for r in rows]


# ------------------------------------------------------------------
# Theme catalog (Phase 2) — the master list the Deck Editor's Themes dropdown
# is populated from. Separate from deck_themes (which is deck_id-scoped
# and still holds the actual main/sub assignments); this table only
# controls what shows up as an OPTION in the dropdown. Removing a theme
# here does not touch any deck's existing deck_themes rows.
# ------------------------------------------------------------------
def list_theme_catalog(conn):
    rows = conn.execute("SELECT theme FROM theme_catalog ORDER BY theme COLLATE NOCASE").fetchall()
    return [r[0] for r in rows]


def add_theme_to_catalog(conn, theme):
    theme = (theme or "").strip()
    if not theme:
        return
    conn.execute("INSERT OR IGNORE INTO theme_catalog (theme) VALUES (?)", (theme,))
    conn.commit()


def remove_theme_from_catalog(conn, theme):
    conn.execute("DELETE FROM theme_catalog WHERE theme=?", (theme,))
    conn.commit()


# ------------------------------------------------------------------
# Game Changer category catalog (Phase 2) — the master list the Card
# Database's Game Changers dropdown is populated from. Separate from
# game_changer_tags (the actual per-card assignments); removing a
# category here does not touch any card's existing assignments.
# ------------------------------------------------------------------
def list_game_changer_categories(conn):
    rows = conn.execute(
        "SELECT category FROM game_changer_category_catalog ORDER BY category COLLATE NOCASE"
    ).fetchall()
    return [r[0] for r in rows]


def add_game_changer_category(conn, category):
    category = (category or "").strip()
    if not category:
        return
    conn.execute("INSERT OR IGNORE INTO game_changer_category_catalog (category) VALUES (?)", (category,))
    conn.commit()


def remove_game_changer_category(conn, category):
    conn.execute("DELETE FROM game_changer_category_catalog WHERE category=?", (category,))
    conn.commit()


# ------------------------------------------------------------------
# Game Changer tag assignment — card_name-keyed (matches
# game_changer_tags' own grain), NOT deck-scoped, unlike card_tags.
# ------------------------------------------------------------------
def get_game_changer_tags_map(conn):
    """{card_name: [category, ...]} for every card currently carrying at
    least one custom Game Changer category."""
    rows = conn.execute("SELECT card_name, tag FROM game_changer_tags").fetchall()
    result = {}
    for card_name, tag in rows:
        result.setdefault(card_name, []).append(tag)
    return result


def set_game_changer_tags(conn, card_name, categories):
    """Replace ALL of one card's Game Changer categories with exactly
    `categories` (list of strings; pass [] to clear). Also registers any
    brand-new category in the master catalog, same as get_or_create_tag
    does for card_tags, so a category typed here immediately shows up in
    the dropdown too."""
    conn.execute("DELETE FROM game_changer_tags WHERE card_name=?", (card_name,))
    seen = set()
    for raw in categories:
        label = (raw or "").strip()
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        conn.execute(
            "INSERT OR IGNORE INTO game_changer_tags (card_name, tag) VALUES (?,?)",
            (card_name, label),
        )
        add_game_changer_category(conn, label)
    conn.commit()


def bulk_set_game_changer_tags(conn, edits):
    """edits: {card_name: [category, ...]}. Returns count of cards whose
    category set actually changed."""
    before = get_game_changer_tags_map(conn)
    changed = 0
    for card_name, categories in edits.items():
        new_set = {c.strip().lower() for c in categories if c and c.strip()}
        old_set = {c.strip().lower() for c in before.get(card_name, [])}
        if new_set != old_set:
            set_game_changer_tags(conn, card_name, categories)
            changed += 1
    return changed


# set_card_salt_score() / bulk_set_salt_scores() (Phase 2) were removed
# in Prompt Pass 5 along with the rest of the EDHREC Salt Score feature —
# see dashboard_lib/queries.py's CARD_COLUMNS comment and PROJECT_STATE.md
# for why. cards.edhrec_salt itself is left in the schema untouched
# (additive policy), so any values entered before this pass aren't lost —
# there just isn't an in-dashboard write path to it anymore.


# ------------------------------------------------------------------
# Mana tag catalog (Prompt Pass 12) — the master list the Card Database's
# Mana Tags dropdown is populated from. Separate from mana_tags (the actual
# per-card assignments); removing a tag here does not touch any card's
# existing assignments. Exact mirror of the Game Changer category catalog
# functions above.
# ------------------------------------------------------------------
def list_mana_tag_catalog(conn):
    rows = conn.execute(
        "SELECT tag FROM mana_tag_catalog ORDER BY tag COLLATE NOCASE"
    ).fetchall()
    return [r[0] for r in rows]


def add_mana_tag_to_catalog(conn, tag):
    tag = (tag or "").strip()
    if not tag:
        return
    conn.execute("INSERT OR IGNORE INTO mana_tag_catalog (tag) VALUES (?)", (tag,))
    conn.commit()


def remove_mana_tag_from_catalog(conn, tag):
    conn.execute("DELETE FROM mana_tag_catalog WHERE tag=?", (tag,))
    conn.commit()


# ------------------------------------------------------------------
# Mana tag assignment (Prompt Pass 12) — card_name-keyed (matches
# mana_tags' own grain), NOT deck-scoped, unlike card_tags. mana_tags has
# no Scryfall-derived flag the way game_changer_tags does, so this is the
# ONLY write path that puts a card in front of the Card Database's Mana
# Tags overview table — exact mirror of the Game Changer tag-assignment
# functions above, just without a scryfall_flag concept.
# ------------------------------------------------------------------
def get_mana_tags_map(conn):
    """{card_name: [tag, ...]} for every card currently carrying at least
    one optimized-mana tag."""
    rows = conn.execute("SELECT card_name, tag FROM mana_tags").fetchall()
    result = {}
    for card_name, tag in rows:
        result.setdefault(card_name, []).append(tag)
    return result


def set_mana_tags(conn, card_name, tags):
    """Replace ALL of one card's mana tags with exactly `tags` (list of
    strings; pass [] to clear). Also registers any brand-new tag in the
    master catalog, so a tag typed here immediately shows up in the
    dropdown too."""
    conn.execute("DELETE FROM mana_tags WHERE card_name=?", (card_name,))
    seen = set()
    for raw in tags:
        label = (raw or "").strip()
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        conn.execute(
            "INSERT OR IGNORE INTO mana_tags (card_name, tag) VALUES (?,?)",
            (card_name, label),
        )
        add_mana_tag_to_catalog(conn, label)
    conn.commit()


def bulk_set_mana_tags(conn, edits):
    """edits: {card_name: [tag, ...]}. Returns count of cards whose tag
    set actually changed."""
    before = get_mana_tags_map(conn)
    changed = 0
    for card_name, tags in edits.items():
        new_set = {t.strip().lower() for t in tags if t and t.strip()}
        old_set = {t.strip().lower() for t in before.get(card_name, [])}
        if new_set != old_set:
            set_mana_tags(conn, card_name, tags)
            changed += 1
    return changed


# ------------------------------------------------------------------
# Location catalog (Prompt Pass 13) — the master list of "standard"
# (non-deck) storage locations offered in the Collection Editor's Location
# dropdown, alongside every tracked deck's own name (see
# queries.location_options()). Removing an entry here does not touch
# any collection row's existing Location value — same pattern as the
# theme/game-changer-category/mana-tag catalogs above.
# ------------------------------------------------------------------
def list_location_catalog(conn):
    rows = conn.execute(
        "SELECT location FROM location_catalog ORDER BY location COLLATE NOCASE"
    ).fetchall()
    return [r[0] for r in rows]


def add_location_to_catalog(conn, location):
    location = (location or "").strip()
    if not location:
        return
    conn.execute("INSERT OR IGNORE INTO location_catalog (location) VALUES (?)", (location,))
    conn.commit()


def remove_location_from_catalog(conn, location):
    conn.execute("DELETE FROM location_catalog WHERE location=?", (location,))
    conn.commit()


# ------------------------------------------------------------------
# Card tags (deck-scoped: card_tags PK is deck_id+scryfall_id+tag_id)
# ------------------------------------------------------------------
def get_card_tags_by_type(conn, deck_id, tag_type):
    """{scryfall_id: [label, ...]} for every card in this deck currently
    carrying a tag of tag_type."""
    rows = conn.execute(
        """SELECT ct.scryfall_id, t.label
           FROM card_tags ct JOIN tags t ON t.tag_id = ct.tag_id
           WHERE ct.deck_id=? AND t.tag_type=?""",
        (deck_id, tag_type),
    ).fetchall()
    result = {}
    for scryfall_id, label in rows:
        result.setdefault(scryfall_id, []).append(label)
    return result


def set_card_tags(conn, deck_id, scryfall_id, tag_type, labels):
    """Replace ALL of one card's tags of `tag_type` (within this deck)
    with exactly `labels` (list of strings; pass [] to clear)."""
    conn.execute(
        """DELETE FROM card_tags
           WHERE deck_id=? AND scryfall_id=? AND tag_id IN (
               SELECT tag_id FROM tags WHERE tag_type=?
           )""",
        (deck_id, scryfall_id, tag_type),
    )
    seen = set()
    for raw in labels:
        label = (raw or "").strip()
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        tag_id = get_or_create_tag(conn, tag_type, label)
        conn.execute(
            "INSERT OR IGNORE INTO card_tags (deck_id, scryfall_id, tag_id) VALUES (?,?,?)",
            (deck_id, scryfall_id, tag_id),
        )
    conn.commit()


def bulk_set_card_tags(conn, deck_id, tag_type, labels_by_scryfall_id):
    """Apply set_card_tags() for many cards in one transaction. Returns
    the number of cards actually touched (rows whose tag set changed)."""
    before = get_card_tags_by_type(conn, deck_id, tag_type)
    changed = 0
    for scryfall_id, labels in labels_by_scryfall_id.items():
        new_set = {l.strip().lower() for l in labels if l and l.strip()}
        old_set = {l.strip().lower() for l in before.get(scryfall_id, [])}
        if new_set != old_set:
            set_card_tags(conn, deck_id, scryfall_id, tag_type, labels)
            changed += 1
    return changed


# ------------------------------------------------------------------
# Deck metadata (name + everything else on the decks row)
# ------------------------------------------------------------------
DECK_META_FIELDS = [
    "commander", "partner", "representative", "color_identity", "deck_type",
    "initially_built", "description", "combos", "tutors", "bracket", "interaction",
]


def create_deck(conn, name, **fields):
    """Returns (deck_id, error). error is None on success."""
    name = (name or "").strip()
    if not name:
        return None, "Deck name can't be empty."
    fields = {k: v for k, v in fields.items() if k in DECK_META_FIELDS}
    fields.setdefault("deck_type", "Commander")
    cols = ["name"] + list(fields.keys())
    placeholders = ", ".join("?" for _ in cols)
    try:
        cur = conn.execute(
            f"INSERT INTO decks ({', '.join(cols)}) VALUES ({placeholders})",
            [name] + list(fields.values()),
        )
    except sqlite3.IntegrityError:
        logger.warning("create_deck: name collision for %r", name)
        return None, f"A deck named '{name}' already exists."
    conn.commit()
    logger.info("Created deck %r (deck_id=%d)", name, cur.lastrowid)
    return cur.lastrowid, None


def update_deck_meta(conn, deck_id, **fields):
    """Updates any of DECK_META_FIELDS — NOT name, which has its own
    rename_deck() below since renaming has knock-on effects (collection
    Location matching) a plain field update doesn't."""
    fields = {k: v for k, v in fields.items() if k in DECK_META_FIELDS}
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE decks SET {set_clause} WHERE deck_id=?", (*fields.values(), deck_id))
    conn.commit()


def set_deck_cover_image(conn, deck_id, relative_path):
    """relative_path is expected to already be relative to the project's
    BASE_DIR (e.g. 'image_cache/deck_covers/12.png'), matching how
    cards.local_image_path is stored — pass None to clear/remove the
    cover image."""
    conn.execute("UPDATE decks SET cover_image_path=? WHERE deck_id=?", (relative_path, deck_id))
    conn.commit()


_RANK_TABLES = {"win_conditions", "strengths", "weaknesses"}


def set_deck_rank_list(conn, deck_id, kind, items):
    """Replace ALL of a deck's ranked list (Win Conditions / Strengths /
    Weaknesses) with exactly `items` — a list of up to 3 dicts, each
    {"label": str, "description": str_or_None}. Rank is assigned by
    position (items[0] -> rank 1, etc). An item with a blank label is
    skipped, so clearing a slot's text and saving removes that rank
    entirely rather than leaving an empty row behind."""
    if kind not in _RANK_TABLES:
        raise ValueError(f"Unknown rank list kind: {kind!r}")
    table = f"deck_{kind}"
    conn.execute(f"DELETE FROM {table} WHERE deck_id=?", (deck_id,))
    for rank, item in enumerate(items[:3], start=1):
        label = (item.get("label") or "").strip()
        if not label:
            continue
        description = (item.get("description") or "").strip() or None
        conn.execute(
            f"INSERT INTO {table} (deck_id, rank, label, description) VALUES (?,?,?,?)",
            (deck_id, rank, label, description),
        )
    conn.commit()


def rename_deck(conn, deck_id, new_name, update_collection_locations=True):
    """Returns (success, error). decks.name is UNIQUE and is matched as
    free text elsewhere (collection.location for "sleeved in this deck"
    tracking, and scripts/migrate.py's tag backup/restore keys off the
    name too) — so a rename here, by default, also updates any
    collection.location rows that exactly matched the old name. It does
    NOT touch decks.csv/deck_mapping.csv: if you later re-run the CSV
    migration, the deck will reappear under its old CSV name unless you
    update the CSV to match."""
    new_name = (new_name or "").strip()
    if not new_name:
        return False, "Name can't be empty."
    row = conn.execute("SELECT name FROM decks WHERE deck_id=?", (deck_id,)).fetchone()
    if not row:
        return False, "Deck not found."
    old_name = row[0]
    if old_name == new_name:
        return True, None
    try:
        conn.execute("UPDATE decks SET name=? WHERE deck_id=?", (new_name, deck_id))
    except sqlite3.IntegrityError:
        logger.warning("rename_deck: name collision for %r", new_name)
        return False, f"A deck named '{new_name}' already exists."
    if update_collection_locations:
        conn.execute("UPDATE collection SET location=? WHERE location=?", (new_name, old_name))
    conn.commit()
    logger.info("Renamed deck %d: %r -> %r", deck_id, old_name, new_name)
    return True, None


# Every table scoped to a single deck via a plain deck_id FK — deleted
# outright when the deck is deleted. game_participants is handled
# separately (detached, not deleted — see delete_deck) since a game
# usually still has OTHER decks' results worth keeping.
_DECK_SCOPED_TABLES = (
    "deck_cards", "deck_changes", "card_tags",
    "deck_themes", "deck_win_conditions", "deck_strengths", "deck_weaknesses",
    # The two Commander Spellbook cache tables (deck_combos /
    # deck_combo_sync) carry a deck_id FK too, so they have to be cleared
    # here or `DELETE FROM decks` fails outright under
    # `PRAGMA foreign_keys = ON`.
    "deck_combos", "deck_combo_sync",
    # maybeboard and deck_swap_queue are the frozen pre-rework backup that
    # deck_changes superseded (see schema.sql) — nothing writes to them any
    # more, but their rows still carry a deck_id FK, so a deleted deck's
    # frozen rows have to go with it or the delete fails the same way.
    "maybeboard", "deck_swap_queue",
)


def delete_deck(conn, deck_id, clear_collection_locations=True):
    """Permanently removes a deck and everything scoped to it (mainboard,
    shortlist/history, card tags, themes, win conditions/strengths/weaknesses).
    Returns (deck_name, error) — error is None on success.

    game_participants rows referencing this deck are deliberately NOT
    deleted outright, to avoid corrupting data that isn't really "this
    deck's": their deck_id is set to NULL instead — the game (and any
    OTHER decks' results in it) stays in the log, this deck's row just
    reverts to a free-text-only record, the same shape an untracked
    opponent's deck already has.

    collection.location rows that exactly matched this deck's name are
    reassigned to DEFAULT_LOCATION ("Box") by default
    (clear_collection_locations=True) so "sleeved in this deck" tracking
    doesn't point at a phantom deck — those cards physically still exist,
    they've just come out of a deck that no longer exists, so they land
    back in general storage rather than losing their Location entirely.
    Set to False to leave that text as-is. (Prompt Pass 13: this used to
    clear the Location to NULL instead of reassigning it — changed so a
    deleted deck's cards don't silently become "no location recorded",
    which is indistinguishable from a card that was never logged at all.)

    Audited as part of Prompt Pass 7 (Commander Game Tracking "Tracked
    Deck Deletion" requirement): the detach-not-delete behavior above
    was already correct going into that pass. The one gap found and
    fixed here is `is_own_deck` — it used to stay 1 on a detached row
    even after deck_id went NULL, which is a mismatch (is_own_deck=1
    implies "this seat maps to a deck row you still track" — this now
    also clears to 0, matching every genuinely-untracked/free-text
    participant row's own shape). `is_own_deck` isn't currently read by
    any query/page — deck_stats/player_stats/games_list all key off
    deck_id/player_name directly — so this is a data-hygiene fix, not a
    behavior change to anything visible today.
    """
    row = conn.execute("SELECT name FROM decks WHERE deck_id=?", (deck_id,)).fetchone()
    if not row:
        return None, "Deck not found."
    deck_name = row[0]

    conn.execute(
        "UPDATE game_participants SET deck_id=NULL, is_own_deck=0 WHERE deck_id=?", (deck_id,)
    )

    for table in _DECK_SCOPED_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE deck_id=?", (deck_id,))

    if clear_collection_locations:
        conn.execute(
            "UPDATE collection SET location=? WHERE location=?", (DEFAULT_LOCATION, deck_name)
        )

    conn.execute("DELETE FROM decks WHERE deck_id=?", (deck_id,))
    conn.commit()
    logger.info("Deleted deck %r (deck_id=%d)", deck_name, deck_id)
    return deck_name, None


# ------------------------------------------------------------------
# Mainboard (deck_cards)
#
# add_deck_card / remove_deck_card are the hand-edit paths (the Deck
# Editor's Mainboard tab). Each one also records an 'applied' row in
# deck_changes when it genuinely adds or removes a card — that is what
# makes a deck's history complete rather than covering only swaps. The
# underscore-prefixed _put/_delete helpers do the same write WITHOUT
# logging, for execute_swap(), whose caller (apply_change) records the
# whole swap as ONE row instead of an add plus a remove.
#
# Only a change in whether a card is on the list is logged. Bumping an
# existing row's quantity (Swamp x4 -> x5) is not an add or a removal, so
# it leaves no history row.
# ------------------------------------------------------------------
def _put_deck_card(conn, deck_id, scryfall_id, quantity):
    conn.execute(
        "INSERT OR REPLACE INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,?)",
        (deck_id, scryfall_id, quantity),
    )
    conn.commit()


def _delete_deck_card(conn, deck_id, scryfall_id):
    conn.execute("DELETE FROM deck_cards WHERE deck_id=? AND scryfall_id=?", (deck_id, scryfall_id))
    conn.commit()


def _deck_card_quantity(conn, deck_id, scryfall_id):
    row = conn.execute(
        "SELECT quantity FROM deck_cards WHERE deck_id=? AND scryfall_id=?", (deck_id, scryfall_id)
    ).fetchone()
    return row[0] if row else None


# Stamped into the notes of an idea retired by _auto_drop_stale_ideas(), so
# History can tell "I decided against this" from "I just did it by hand".
AUTO_DROP_NOTE = "auto-dropped: added to the mainboard by hand"


def _auto_drop_stale_ideas(conn, deck_id, scryfall_id):
    """Retire a deck's open shortlist rows for a card that has just been
    added to its mainboard by hand (Phase 2).

    Without this, adding a card directly leaves the idea that asked for it
    sitting on the shortlist forever, and the Workbench counts work that is
    already done. Dropped rather than left flagged because the shortlist is
    a to-do list and the to-do is finished; dropped rather than marked
    applied because add_deck_card() already logs an `applied` row for the
    hand edit, and one real event should be one history row.

    Matched across printings (oracle_id, name fallback): an idea for the
    cheap printing is satisfied by hand-adding the foil. Returns the
    change_ids retired, and does nothing on an empty shortlist.

    Only rows the user could still act on (idea/planned) are touched;
    reopen_change() restores one if the hand-add was the mistake."""
    oracle_ids = q.oracle_ids_for_card(conn, scryfall_id=scryfall_id)
    if oracle_ids:
        placeholders = ",".join("?" for _ in oracle_ids)
        match_sql = f"""dc.add_scryfall_id IN (
                            SELECT scryfall_id FROM cards WHERE oracle_id IN ({placeholders}))"""
        params = tuple(oracle_ids)
    else:
        # No oracle_id synced for this printing: fall back to its own id
        # plus an exact name match, which also catches a row that stored
        # only a typed name (every swap backfilled from the old queue).
        name = _card_name(conn, scryfall_id)
        match_sql = """(dc.add_scryfall_id = ?
                        OR (dc.add_scryfall_id IS NULL AND dc.add_name = ? COLLATE NOCASE))"""
        params = (scryfall_id, name)
    rows = conn.execute(
        f"""SELECT dc.change_id FROM deck_changes dc
            WHERE dc.deck_id = ? AND dc.status IN ('idea','planned') AND {match_sql}""",
        (deck_id, *params),
    ).fetchall()
    change_ids = [r[0] for r in rows]
    for change_id in change_ids:
        conn.execute(
            """UPDATE deck_changes
                  SET status='dropped', resolved_at=CURRENT_TIMESTAMP,
                      notes = CASE
                          WHEN notes IS NULL OR TRIM(notes) = '' THEN ?
                          ELSE notes || ' — ' || ?
                      END
                WHERE change_id = ?""",
            (AUTO_DROP_NOTE, AUTO_DROP_NOTE, change_id),
        )
    if change_ids:
        conn.commit()
        logger.info("Auto-dropped %d stale shortlist row(s) on deck %s: %s",
                    len(change_ids), deck_id, change_ids)
    return change_ids


def add_deck_card(conn, deck_id, scryfall_id, quantity=1):
    existing = _deck_card_quantity(conn, deck_id, scryfall_id)
    _put_deck_card(conn, deck_id, scryfall_id, quantity)
    if existing is None:
        _log_applied_change(conn, deck_id, add_scryfall_id=scryfall_id, quantity=quantity)
        # Only on a genuine join: a quantity bump doesn't retire anything,
        # and execute_swap() goes through _put_deck_card, so applying a
        # planned swap never auto-drops its own row.
        _auto_drop_stale_ideas(conn, deck_id, scryfall_id)


def remove_deck_card(conn, deck_id, scryfall_id):
    existing = _deck_card_quantity(conn, deck_id, scryfall_id)
    _delete_deck_card(conn, deck_id, scryfall_id)
    if existing is not None:
        _log_applied_change(conn, deck_id, remove_scryfall_id=scryfall_id, quantity=existing)


def bulk_update_deck_cards(conn, deck_id, updates):
    """updates: {scryfall_id: new_quantity} — a card mapped to None or a
    non-positive quantity is removed instead of set. Returns the number
    of cards actually changed."""
    current = dict(
        conn.execute("SELECT scryfall_id, quantity FROM deck_cards WHERE deck_id=?", (deck_id,)).fetchall()
    )
    changed = 0
    for scryfall_id, qty in updates.items():
        old_qty = current.get(scryfall_id)
        if qty is None or qty <= 0:
            if old_qty is not None:
                remove_deck_card(conn, deck_id, scryfall_id)
                changed += 1
            continue
        if qty != old_qty:
            add_deck_card(conn, deck_id, scryfall_id, qty)
            changed += 1
    return changed


# ------------------------------------------------------------------
# Deck changes — the idea -> planned -> applied / dropped lifecycle
# (Workbench rework, Phase 1; table in schema.sql, plan in
# IMPLEMENTATION_PLAN.md). One table replaces what used to be the
# maybeboard, the Swap Manager queue, and nothing at all for history:
#
#   idea     a card under consideration; may or may not name a card it
#            would replace, and may or may not be owned
#   planned  paired with a card to cut and staged — nothing is written to
#            deck_cards / collection until apply_change()
#   applied  executed; immutable history
#   dropped  rejected, kept on purpose so it isn't re-evaluated from scratch
#
# Every transition below raises ValueError with a user-presentable message
# when it can't happen (wrong state, nothing to replace, ...) rather than
# silently doing nothing, so the page can show the reason.
# ------------------------------------------------------------------
OPEN_CHANGE_STATUSES = ("idea", "planned")

# The columns a caller may edit directly. status and the three timestamps
# are deliberately absent: they only move through the transition functions.
_CHANGE_EDITABLE = (
    "add_scryfall_id", "add_name", "remove_scryfall_id", "remove_name",
    "quantity", "review_flag", "notes",
)


def _query_dicts(conn, sql, params=()):
    """Rows as plain dicts regardless of the connection's row_factory (the
    app's connection uses sqlite3.Row; a bare one doesn't)."""
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_change(conn, change_id):
    rows = _query_dicts(conn, "SELECT * FROM deck_changes WHERE change_id=?", (change_id,))
    return rows[0] if rows else None


def list_changes(conn, deck_id=None, statuses=OPEN_CHANGE_STATUSES):
    """Change rows as dicts, oldest-planned first. deck_id=None spans every
    deck (what the Workbench's global queue will use)."""
    statuses = tuple(statuses)
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    sql = f"SELECT * FROM deck_changes WHERE status IN ({placeholders})"
    params = list(statuses)
    if deck_id is not None:
        sql += " AND deck_id=?"
        params.append(deck_id)
    sql += " ORDER BY COALESCE(planned_at, created_at), change_id"
    return _query_dicts(conn, sql, params)


def _require_change(conn, change_id, *statuses):
    row = get_change(conn, change_id)
    if row is None:
        raise ValueError(f"Change #{change_id} no longer exists.")
    if statuses and row["status"] not in statuses:
        raise ValueError(f"Change #{change_id} is {row['status']!r}, not {' / '.join(statuses)}.")
    return row


def _card_name(conn, scryfall_id):
    if not scryfall_id:
        return None
    row = conn.execute("SELECT name FROM cards WHERE scryfall_id=?", (scryfall_id,)).fetchone()
    return row[0] if row else None


def _open_change_for_card(conn, deck_id, add_scryfall_id, exclude_change_id=-1):
    """change_id of an idea/planned row already adding this exact printing
    to this deck, or None. There is no UNIQUE constraint to lean on (the old
    maybeboard's primary key did this job): a deck_changes row has to be
    allowed to repeat a card across its history, so duplicates among OPEN
    rows are prevented here instead."""
    row = conn.execute(
        """SELECT change_id FROM deck_changes
           WHERE deck_id=? AND add_scryfall_id=? AND status IN ('idea','planned')
             AND change_id != ?
           LIMIT 1""",
        (deck_id, add_scryfall_id, exclude_change_id),
    ).fetchone()
    return row[0] if row else None


def _open_removal_for_card(conn, deck_id, remove_scryfall_id):
    """change_id of an idea/planned row already cutting this exact printing
    from this deck, or None — the removal-side twin of
    _open_change_for_card() above.

    Phase 6 needs it because a dismantle is the first path that stages pure
    removals in bulk: staging the same deck twice (say, to feed two
    different decks) would otherwise queue two cuts of one card, and
    applying both would try to remove a card the first cut already took off
    the list."""
    row = conn.execute(
        """SELECT change_id FROM deck_changes
           WHERE deck_id=? AND remove_scryfall_id=? AND status IN ('idea','planned')
           LIMIT 1""",
        (deck_id, remove_scryfall_id),
    ).fetchone()
    return row[0] if row else None


def add_change(conn, deck_id, *, add_scryfall_id=None, add_name=None,
               remove_scryfall_id=None, remove_name=None, quantity=1,
               review_flag=0, notes=None, status="idea"):
    """Insert an idea (default) or an already-paired planned swap. Returns
    the new change_id. Raises ValueError for a duplicate open row on the
    same card, a planned row with no card to cut, or a row that does
    nothing at all."""
    if status not in OPEN_CHANGE_STATUSES:
        raise ValueError("A new change starts as an idea or a planned swap.")
    if not (add_scryfall_id or add_name or remove_scryfall_id):
        raise ValueError("A change has to add a card, remove one, or both.")
    if status == "planned" and not remove_scryfall_id:
        raise ValueError("A planned swap needs a card to replace.")
    if int(quantity) < 1:
        raise ValueError("Quantity must be at least 1.")
    if add_scryfall_id and _open_change_for_card(conn, deck_id, add_scryfall_id):
        raise ValueError(f"{add_name or add_scryfall_id} is already on this deck's shortlist.")

    cur = conn.execute(
        """INSERT INTO deck_changes
               (deck_id, status, add_scryfall_id, add_name, remove_scryfall_id,
                remove_name, quantity, review_flag, notes, planned_at)
           VALUES (?,?,?,?,?,?,?,?,?, CASE WHEN ? = 'planned' THEN CURRENT_TIMESTAMP END)""",
        (deck_id, status, add_scryfall_id, add_name, remove_scryfall_id,
         remove_name, int(quantity), 1 if review_flag else 0, notes, status),
    )
    conn.commit()
    return cur.lastrowid


def _log_applied_change(conn, deck_id, add_scryfall_id=None, remove_scryfall_id=None, quantity=1):
    """Record an already-completed hand edit (a mainboard add or remove)
    as an 'applied' row, so history covers more than swaps. Not for the
    shortlist flow: apply_change() flips its own row to applied instead."""
    conn.execute(
        """INSERT INTO deck_changes
               (deck_id, status, add_scryfall_id, add_name, remove_scryfall_id,
                remove_name, quantity, resolved_at)
           VALUES (?, 'applied', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (deck_id, add_scryfall_id, _card_name(conn, add_scryfall_id),
         remove_scryfall_id, _card_name(conn, remove_scryfall_id), quantity),
    )
    conn.commit()


def update_change(conn, change_id, **fields):
    fields = {k: v for k, v in fields.items() if k in _CHANGE_EDITABLE}
    if not fields:
        return
    if "review_flag" in fields:
        fields["review_flag"] = 1 if fields["review_flag"] else 0
    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE deck_changes SET {set_clause} WHERE change_id=?",
        (*fields.values(), change_id),
    )
    conn.commit()


def bulk_update_changes(conn, deck_id, updates):
    """updates: {change_id: {field: value, ...}} limited to the editable
    columns. Diffed against current state so untouched rows aren't
    rewritten; rows that are no longer open (applied/dropped since the page
    loaded) are skipped rather than edited. Returns the count changed."""
    current = {r["change_id"]: r for r in list_changes(conn, deck_id)}
    changed = 0
    for change_id, edit in updates.items():
        old = current.get(change_id)
        if old is None:
            continue
        new_vals = {k: v for k, v in edit.items() if k in _CHANGE_EDITABLE}
        if "review_flag" in new_vals:
            new_vals["review_flag"] = 1 if new_vals["review_flag"] else 0
        if any(old.get(k) != v for k, v in new_vals.items()):
            update_change(conn, change_id, **new_vals)
            changed += 1
    return changed


def promote_change(conn, change_id, remove_scryfall_id=None, remove_name=None, quantity=None):
    """idea -> planned. Needs a card to replace: the row's own, or one passed
    in here (which then overwrites it). This is what finally consumes the
    replace target an idea carries — the old maybeboard recorded one and
    nothing ever read it."""
    row = _require_change(conn, change_id, "idea")
    target = remove_scryfall_id or row["remove_scryfall_id"]
    if not target:
        raise ValueError("Pick a card for this to replace before planning it.")
    target_name = remove_name or _card_name(conn, target) or row["remove_name"]
    conn.execute(
        """UPDATE deck_changes
           SET status='planned', planned_at=CURRENT_TIMESTAMP,
               remove_scryfall_id=?, remove_name=?, quantity=COALESCE(?, quantity)
           WHERE change_id=?""",
        (target, target_name, int(quantity) if quantity is not None else None, change_id),
    )
    conn.commit()


def demote_change(conn, change_id):
    """planned -> idea, keeping the pairing so re-planning it is one click."""
    _require_change(conn, change_id, "planned")
    conn.execute(
        "UPDATE deck_changes SET status='idea', planned_at=NULL WHERE change_id=?", (change_id,)
    )
    conn.commit()


def drop_change(conn, change_id):
    """idea/planned -> dropped. Never deletes: the row is the memory of why
    a card lost, so it isn't re-evaluated from scratch next month."""
    _require_change(conn, change_id, *OPEN_CHANGE_STATUSES)
    conn.execute(
        "UPDATE deck_changes SET status='dropped', resolved_at=CURRENT_TIMESTAMP WHERE change_id=?",
        (change_id,),
    )
    conn.commit()


def resolve_contention(conn, change_ids, keep_change_id=None, drop_change_ids=()):
    """Settle a Workbench conflict: several open changes want one card, and
    only one can have it.

    `change_ids` is every row competing for the card. `keep_change_id`
    names the winner, which drops all the others; `drop_change_ids` drops
    named rows without picking a winner. The two combine — picking a winner
    and separately ticking a row is not a contradiction, the winner just
    survives either way.

    Returns {"kept", "dropped", "errors"}: the change_id kept, the ones
    dropped, and [(change_id, message)] for any that couldn't be (already
    applied, say, because another tab applied it since this page rendered).
    Nothing is dropped unless something was asked for, so a stray click
    can't quietly clear a conflict.

    Raises ValueError for a keep that isn't one of the competing rows, or
    for a request that would drop the winner — both mean the caller's view
    of the conflict is stale, and guessing which half was meant is worse
    than refusing."""
    change_ids = [int(c) for c in change_ids]
    drop_requested = [int(c) for c in drop_change_ids]
    if keep_change_id is not None:
        keep_change_id = int(keep_change_id)
        if keep_change_id not in change_ids:
            raise ValueError("The row to keep isn't one of the changes competing for this card.")
        if keep_change_id in drop_requested:
            raise ValueError("That row is marked both keep and drop — pick one.")
    stray = [c for c in drop_requested if c not in change_ids]
    if stray:
        raise ValueError(f"Not competing for this card: {stray}")

    to_drop = list(drop_requested)
    if keep_change_id is not None:
        to_drop += [c for c in change_ids if c != keep_change_id and c not in to_drop]

    dropped, errors = [], []
    for change_id in dict.fromkeys(to_drop):   # de-duplicated, order kept
        try:
            drop_change(conn, change_id)
            dropped.append(change_id)
        except ValueError as exc:
            errors.append((change_id, str(exc)))
    if dropped:
        logger.info("Resolved contention: kept %s, dropped %s", keep_change_id, dropped)
    return {"kept": keep_change_id, "dropped": dropped, "errors": errors}


def reopen_change(conn, change_id):
    """dropped -> idea. Refuses if the same card has since been re-added to
    the shortlist, since two open rows for one card is the state
    add_change() exists to prevent."""
    row = _require_change(conn, change_id, "dropped")
    if row["add_scryfall_id"] and _open_change_for_card(conn, row["deck_id"], row["add_scryfall_id"]):
        raise ValueError(f"{row['add_name'] or 'That card'} is already back on the shortlist.")
    conn.execute(
        "UPDATE deck_changes SET status='idea', planned_at=NULL, resolved_at=NULL WHERE change_id=?",
        (change_id,),
    )
    conn.commit()


def _unsleeve(conn, deck_name, card_name, dest_location, quantity=None):
    """Move lots of `card_name` currently sleeved in `deck_name` out to
    `dest_location`, up to `quantity` copies (all of them when None).
    Returns [collection_id, ...] of the lots now at the destination.

    This is the "return to storage" step execute_swap() has always done for
    a swap's removed card, factored out so a pure removal and a dismantle's
    direct transfer can reuse it — the difference between them is only where
    the copies land, which is what `dest_location` carries.

    Matched on name rather than printing: the lot sleeved in the deck may be
    a different printing from the one on the mainboard, and it is still the
    copy physically coming out of these sleeves."""
    if not card_name:
        return []
    rows = conn.execute(
        """SELECT col.collection_id, COALESCE(col.quantity, 1)
           FROM collection col JOIN cards c ON c.scryfall_id = col.scryfall_id
           WHERE c.name = ? COLLATE NOCASE AND col.location = ?
           ORDER BY col.collection_id""",
        (card_name, deck_name),
    ).fetchall()
    moved, taken = [], 0
    for collection_id, have in rows:
        if quantity is not None and taken >= quantity:
            break
        take = int(have) if quantity is None else min(int(have), quantity - taken)
        moved.append(move_lot(conn, collection_id, dest_location, quantity=take))
        taken += take
    return moved


def _sleeve_here(conn, deck_name, add_scryfall_id, add_card_name, quantity=1):
    """Put a copy of a card into `deck_name`'s sleeves: reuse the first
    lot that isn't already sleeved in some deck, else create a starter lot.
    Returns {"assigned_lot_id", "created_lot"}.

    The same rule execute_swap() uses, and the same reason: a card you own
    whose every copy is sleeved in another deck gets a fresh lot here rather
    than silently stealing one out of that deck's sleeves. A copy already
    sleeved in THIS deck (the end state of a direct transfer, whose removal
    side moved the lot here first) is left alone — there is nothing to do."""
    status = q.card_inventory_status(conn, add_card_name, deck_name=deck_name,
                                     scryfall_id=add_scryfall_id)
    if status["in_this_deck_qty"]:
        return {"assigned_lot_id": None, "created_lot": False}
    if status["available_lots"]:
        lot_id = status["available_lots"][0]["collection_id"]
        update_collection_lot(conn, lot_id, location=deck_name)
        return {"assigned_lot_id": lot_id, "created_lot": False}
    lot_id = add_collection_lot(
        conn, add_scryfall_id,
        quantity=quantity, foil=False, location=deck_name,
        date_acquired=datetime.date.today().isoformat(), price_paid=None,
        source="Auto-added (swap manager)",
    )
    return {"assigned_lot_id": lot_id, "created_lot": True}


def apply_change(conn, change_id, add_scryfall_id=None):
    """Execute one planned change against the deck and collection and mark
    the row 'applied'. Three shapes, dispatched on which sides the row has:

      add + remove   a swap — execute_swap(), as this has always done
      remove only    a pure cut (Phase 6: a dismantle's removals). The card
                     leaves the mainboard and its sleeved copies move to
                     `dest_location`, or to DEFAULT_LOCATION when that is
                     NULL. Nothing is added, so the deck ends a card down
      add only       a pure addition (Phase 6: a dismantle feeding a target
                     deck). The card joins the mainboard and gets sleeved
                     here; nothing is cut

    A row with an add_name but no linked printing is NOT a pure removal —
    it is a swap whose add side was never resolved, and it still raises
    rather than quietly cutting a card and adding nothing.

    `add_scryfall_id` supplies the printing for a row that has none yet —
    which is every row backfilled from the old swap queue, since that stored
    only a typed name. Resolution (a live Scryfall lookup when needed) is the
    caller's job, same division of labour as execute_swap.

    Raises ValueError, writing nothing, if the row isn't planned, the add
    card can't be resolved, or the card to cut is no longer in the mainboard
    (applying anyway would add a card without cutting one and leave the deck
    a card over, with an 'applied' row claiming otherwise).

    Returns execute_swap()'s shape in every case — {"moved_out",
    "assigned_lot_id", "created_lot"} — so one caller can report all three.

    NOT atomic across the whole change: the writes commit as they go, so an
    unexpected failure part-way (a database error, not one of the checks
    above) can leave the deck changed while the row is still 'planned'. The
    checks above run first precisely so that the expected failures happen
    before anything is touched."""
    row = _require_change(conn, change_id, "planned")
    add_sid = row["add_scryfall_id"] or add_scryfall_id
    remove_sid = row["remove_scryfall_id"]
    # An add side that exists but hasn't resolved to a printing is an error,
    # not an absent add side — see the docstring.
    if not add_sid and (row["add_name"] or "").strip():
        raise ValueError(f"{row['add_name']} isn't linked to a printing yet.")
    if not add_sid and not remove_sid:
        raise ValueError("The card to add isn't linked to a printing yet.")

    deck_id = row["deck_id"]
    deck_name = conn.execute("SELECT name FROM decks WHERE deck_id=?", (deck_id,)).fetchone()[0]
    add_card_name = _card_name(conn, add_sid) or row["add_name"]
    remove_card_name = _card_name(conn, remove_sid) or row["remove_name"]
    quantity = row["quantity"] or 1
    # Phase 6 rows name their destination; everything older means storage.
    dest = (row["dest_location"] if "dest_location" in row else None) or DEFAULT_LOCATION

    if remove_sid and _deck_card_quantity(conn, deck_id, remove_sid) is None:
        raise ValueError(
            f"{remove_card_name} is no longer in this deck's mainboard, so there is nothing to "
            "replace. Send it back to an idea or drop it."
        )

    if add_sid and remove_sid:
        outcome = execute_swap(
            conn, deck_id, deck_name,
            remove_scryfall_id=remove_sid, remove_card_name=remove_card_name,
            add_scryfall_id=add_sid, add_card_name=add_card_name, quantity=quantity,
            default_location=dest,
        )
    elif remove_sid:
        _delete_deck_card(conn, deck_id, remove_sid)
        moved = _unsleeve(conn, deck_name, remove_card_name, dest)
        outcome = {"moved_out": bool(moved), "assigned_lot_id": None, "created_lot": False}
    else:
        _put_deck_card(conn, deck_id, add_sid, quantity)
        outcome = {"moved_out": False,
                   **_sleeve_here(conn, deck_name, add_sid, add_card_name, quantity)}

    conn.execute(
        """UPDATE deck_changes
           SET status='applied', resolved_at=CURRENT_TIMESTAMP,
               add_scryfall_id=?, add_name=?, remove_name=?
           WHERE change_id=?""",
        (add_sid, add_card_name, remove_card_name, change_id),
    )
    conn.commit()
    return outcome


def apply_changes(conn, change_ids, resolve_add=None):
    """Apply several planned swaps in planned order. One failing doesn't stop
    the rest. `resolve_add(name) -> (scryfall_id, was_new, error)` links a row
    that has no printing yet (card_resolver.resolve_or_fetch_card fits);
    without it such a row just fails with a clear message.

    Returns one dict per change: {"change_id", "ok", "add_name", "remove_name",
    "was_new", "outcome", "error"}. `outcome` is execute_swap's result."""
    rows = [get_change(conn, cid) for cid in change_ids]
    rows = sorted((r for r in rows if r), key=lambda r: (r["planned_at"] or r["created_at"], r["change_id"]))
    results = []
    for row in rows:
        result = {
            "change_id": row["change_id"], "ok": False,
            "add_name": row["add_name"] or _card_name(conn, row["add_scryfall_id"]),
            "remove_name": row["remove_name"] or _card_name(conn, row["remove_scryfall_id"]),
            "was_new": False, "outcome": None, "error": None,
        }
        try:
            resolved = None
            # Only an UNRESOLVED add side needs resolving; a Phase 6 pure
            # removal has no add side at all and must not be sent to
            # resolve_add() as a None name.
            if (row["status"] == "planned" and not row["add_scryfall_id"]
                    and (row["add_name"] or "").strip()):
                if resolve_add is None:
                    raise ValueError(f"{result['add_name']} isn't linked to a printing yet.")
                resolved, result["was_new"], err = resolve_add(row["add_name"])
                if err:
                    raise ValueError(err)
            result["outcome"] = apply_change(conn, row["change_id"], add_scryfall_id=resolved)
            result["ok"] = True
        except ValueError as exc:
            result["error"] = str(exc)
        results.append(result)
    return results


# ------------------------------------------------------------------
# Themes
# ------------------------------------------------------------------
def add_deck_theme(conn, deck_id, theme, role):
    theme = (theme or "").strip()
    if not theme or role not in ("main", "sub"):
        return
    conn.execute(
        "INSERT OR IGNORE INTO deck_themes (deck_id, theme, role) VALUES (?,?,?)",
        (deck_id, theme, role),
    )
    conn.commit()


def remove_deck_theme(conn, deck_id, theme, role):
    conn.execute(
        "DELETE FROM deck_themes WHERE deck_id=? AND theme=? AND role=?", (deck_id, theme, role)
    )
    conn.commit()


# ------------------------------------------------------------------
# Collection
# ------------------------------------------------------------------
COLLECTION_FIELDS = ["quantity", "foil", "location", "date_acquired", "price_paid", "source"]


def add_collection_lot(conn, scryfall_id, **fields):
    fields = {k: fields.get(k) for k in COLLECTION_FIELDS}
    fields["foil"] = 1 if fields.get("foil") else 0
    cur = conn.execute(
        """INSERT INTO collection (scryfall_id, quantity, foil, location, date_acquired, price_paid, source)
           VALUES (?,?,?,?,?,?,?)""",
        (scryfall_id, fields["quantity"], fields["foil"], fields["location"],
         fields["date_acquired"], fields["price_paid"], fields["source"]),
    )
    conn.commit()
    return cur.lastrowid


def update_collection_lot(conn, collection_id, **fields):
    fields = {k: v for k, v in fields.items() if k in COLLECTION_FIELDS}
    if not fields:
        return
    if "foil" in fields:
        fields["foil"] = 1 if fields["foil"] else 0
    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE collection SET {set_clause} WHERE collection_id=?",
        (*fields.values(), collection_id),
    )
    conn.commit()


def move_lot(conn, collection_id, location, quantity=None):
    """Set a lot's Location, moving only `quantity` copies when that is
    fewer than the lot holds (Workbench reconciliation: a lot of 3 where the
    deck needs 1). The remainder stays behind as its own lot at the old
    location, with the same printing/finish/source/date; `price_paid` is
    split in proportion, on the evidence that it's a lot total (a 2-copy lot
    paid $0.69 for a $0.53 card). A NULL-quantity (untracked) lot can't be
    split and always moves whole. Returns the collection_id of the lot now
    at `location`."""
    row = conn.execute(
        "SELECT scryfall_id, quantity, foil, location, date_acquired, price_paid, source "
        "FROM collection WHERE collection_id=?", (collection_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"No collection lot #{collection_id}.")
    sid, have, foil, _old_loc, acquired, paid, source = row
    if quantity is None or have is None or quantity >= have:
        update_collection_lot(conn, collection_id, location=location)
        return collection_id
    if quantity < 1:
        raise ValueError("Move at least one copy.")
    moved_paid = None if paid is None else round(paid * quantity / have, 2)
    left_paid = None if paid is None else round(paid - moved_paid, 2)
    conn.execute(
        "UPDATE collection SET quantity=?, price_paid=? WHERE collection_id=?",
        (have - quantity, left_paid, collection_id),
    )
    cur = conn.execute(
        """INSERT INTO collection (scryfall_id, quantity, foil, location, date_acquired,
                                   price_paid, source) VALUES (?,?,?,?,?,?,?)""",
        (sid, quantity, foil, location, acquired, moved_paid, source),
    )
    conn.commit()
    return cur.lastrowid


def remove_collection_lot(conn, collection_id):
    conn.execute("DELETE FROM collection WHERE collection_id=?", (collection_id,))
    conn.commit()


def prune_collection(conn):
    """Deletes any collection lot that meets ALL three conditions:
      a) no assigned location (NULL or blank/whitespace-only)
      b) a quantity of 0 or "none" (i.e. NULL)
      c) not included in any existing deck list — checked against both
         a deck's mainboard (deck_cards) and its open shortlist (ideas and
         planned changes in deck_changes), so a card still under
         consideration for a deck isn't swept away just because it isn't
         physically located anywhere yet.

    Called automatically as part of the Card Database page's Card Data
    "Refresh" action (see dashboard_lib/refresh.py) so stale collection rows for
    cards you no longer own and aren't running anywhere get cleaned up
    without a separate manual step. Returns a list of
    (collection_id, card_name) tuples for whatever was removed, so
    callers can report it."""
    rows = conn.execute(
        """SELECT col.collection_id, c.name
           FROM collection col
           JOIN cards c ON c.scryfall_id = col.scryfall_id
           WHERE (col.location IS NULL OR TRIM(col.location) = '')
             AND (col.quantity IS NULL OR col.quantity = 0)
             AND col.scryfall_id NOT IN (SELECT scryfall_id FROM deck_cards)
             -- add_scryfall_id is NULL for any idea/planned row not yet
             -- linked to a printing (every backfilled swap-queue row, for
             -- one). `x NOT IN (subquery containing a NULL)` is NULL, not
             -- true, so without this filter a single unlinked row would make
             -- the whole condition false and silently disable pruning.
             AND col.scryfall_id NOT IN (
                 SELECT add_scryfall_id FROM deck_changes
                 WHERE status IN ('idea', 'planned') AND add_scryfall_id IS NOT NULL)
           ORDER BY c.name COLLATE NOCASE"""
    ).fetchall()
    if not rows:
        return []
    ids = [r[0] for r in rows]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM collection WHERE collection_id IN ({placeholders})", ids)
    conn.commit()
    logger.info("Pruned %d collection lot(s): %s", len(rows), ", ".join(r[1] for r in rows))
    return [(r[0], r[1]) for r in rows]


def _validate_game_participants(participants):
    """Shared create_game()/update_game() validation (Prompt Pass 7):
    at least one usable participant, and — among participants that
    actually have a deck — exactly one winner. Returns an error
    string, or None if the list is fine to save.

    This is a defense-in-depth backstop: the Commander Game Tracking
    page's own form-level validation (which can give a friendlier,
    seat-numbered error message) is expected to catch these cases
    first, but this layer means a game can't be logged with zero or
    multiple winners no matter how it's called. Note this only applies
    going forward — historical games imported by migrate.py insert
    directly via SQL and never pass through here, so any real draws
    (zero winners) already in games_played.csv are preserved as-is."""
    usable = [p for p in participants if (p.get("deck_name") or "").strip()]
    if not usable:
        return "Add at least one deck/participant before logging the game."
    winner_count = sum(1 for p in usable if p.get("is_winner"))
    if winner_count == 0:
        return "Select a winner before logging the game."
    if winner_count > 1:
        return "Only one seat can be marked as the winner."
    return None


def _insert_game_participants(conn, game_id, participants):
    """Shared insert step for create_game()/update_game(): participants
    is assumed already filtered/validated. Seat number is assigned
    1..N by list position; deck_id is None for opponent decks not in
    the tracked `decks` table (free-text entry), and is_own_deck is
    derived from that, matching migrate.py's original convention."""
    for seat, p in enumerate(participants, start=1):
        deck_id = p.get("deck_id")
        conn.execute(
            """INSERT INTO game_participants
               (game_id, deck_name, deck_id, is_own_deck, is_winner, seat, player_name)
               VALUES (?,?,?,?,?,?,?)""",
            (
                game_id,
                p["deck_name"].strip(),
                deck_id,
                1 if deck_id is not None else 0,
                1 if p.get("is_winner") else 0,
                seat,
                (p.get("player_name") or "").strip() or None,
            ),
        )


def create_game(conn, date, note, participants):
    """Logs one Commander game. participants: ordered list of dicts,
    each {"deck_name": str, "deck_id": int_or_None, "is_winner": bool,
    "player_name": str_or_None}. Returns (game_id, error) — error is a
    message and game_id is None if the participant list doesn't pass
    _validate_game_participants() (Prompt Pass 7: a winner is now
    required, not just "at most one" — see that function's docstring
    for the historical-data caveat)."""
    participants = [p for p in participants if (p.get("deck_name") or "").strip()]
    err = _validate_game_participants(participants)
    if err:
        return None, err

    cur = conn.execute("INSERT INTO games (date, note) VALUES (?,?)", (date or None, note or None))
    game_id = cur.lastrowid
    _insert_game_participants(conn, game_id, participants)
    conn.commit()
    return game_id, None


def update_game(conn, game_id, date, note, participants):
    """Edits an existing logged game in place (Prompt Pass 7): updates
    the games row's date/note, then replaces its entire
    game_participants set (delete + reinsert, same whole-list-replace
    pattern as set_deck_rank_list()) rather than diffing seat by seat —
    game_participants has no other table pointing at it via FK, so a
    full reassignment is simple and safe. participants: same shape as
    create_game()'s. Returns (success, error)."""
    row = conn.execute("SELECT game_id FROM games WHERE game_id=?", (game_id,)).fetchone()
    if not row:
        return False, "Game not found."

    participants = [p for p in participants if (p.get("deck_name") or "").strip()]
    err = _validate_game_participants(participants)
    if err:
        return False, err

    conn.execute("UPDATE games SET date=?, note=? WHERE game_id=?", (date or None, note or None, game_id))
    conn.execute("DELETE FROM game_participants WHERE game_id=?", (game_id,))
    _insert_game_participants(conn, game_id, participants)
    conn.commit()
    return True, None


def delete_game(conn, game_id):
    """Removes a logged game and all of its participant rows — for
    correcting a mis-entered log, not part of normal play. Returns
    (deleted, error)."""
    row = conn.execute("SELECT game_id FROM games WHERE game_id=?", (game_id,)).fetchone()
    if not row:
        return False, "Game not found."
    conn.execute("DELETE FROM game_participants WHERE game_id=?", (game_id,))
    conn.execute("DELETE FROM games WHERE game_id=?", (game_id,))
    conn.commit()
    logger.info("Deleted logged game %d", game_id)
    return True, None


# ------------------------------------------------------------------
# Deck Building Auto-Add (Prompt Pass 6) — when a card added to a deck's
# mainboard isn't tracked in the collection AT ALL yet, give it a starter
# collection lot automatically instead of letting the deck and the
# collection quietly drift apart. Only called from the Deck Editor's
# Mainboard "Add to mainboard" action (see pages/4_Deck_Editor.py) — NOT from the
# Shortlist add flow, since a shortlisted idea is explicitly "under
# consideration", not yet a real deck inclusion, so assuming ownership
# there would overstate the collection.
# ------------------------------------------------------------------
def collection_has_card(conn, scryfall_id):
    """True if at least one collection lot already exists for this exact
    printing (scryfall_id) — the presence test auto_add_to_collection()
    uses to decide whether a starter lot is needed."""
    row = conn.execute(
        "SELECT 1 FROM collection WHERE scryfall_id=? LIMIT 1", (scryfall_id,)
    ).fetchone()
    return row is not None


def auto_add_to_collection(conn, scryfall_id, quantity=1, location=None):
    """If `scryfall_id` has no collection lot at all yet, create one with
    `quantity` (matching however many were just added to the deck),
    `location` defaulted to the deck's name (the same "sleeved in this
    deck" free-text convention collection.location already uses
    everywhere else), no foil, today's date, and a distinct `source` tag
    so these auto-created lots are easy to spot/audit later if you want
    to fill in real acquisition details (price paid, actual foil status,
    etc.).

    Returns the new collection_id, or None if a lot already existed and
    nothing was added — existing lots for this printing are never
    touched, duplicated, or have their quantity bumped."""
    if collection_has_card(conn, scryfall_id):
        return None
    return add_collection_lot(
        conn, scryfall_id,
        quantity=quantity,
        foil=False,
        location=location,
        date_acquired=datetime.date.today().isoformat(),
        price_paid=None,
        source="Auto-added (deck build)",
    )


def bulk_update_collection(conn, updates):
    """updates: {collection_id: {field: value, ..., "_delete": bool}}.
    Returns count changed. Deletes are diffed as unconditional (a row
    flagged _delete is always removed); field updates are diffed against
    current DB state so untouched rows aren't rewritten needlessly."""
    ids = list(updates.keys())
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    current_rows = conn.execute(
        f"SELECT collection_id, quantity, foil, location, date_acquired, price_paid, source "
        f"FROM collection WHERE collection_id IN ({placeholders})",
        ids,
    ).fetchall()
    current = {
        r[0]: {"quantity": r[1], "foil": r[2], "location": r[3],
               "date_acquired": r[4], "price_paid": r[5], "source": r[6]}
        for r in current_rows
    }
    changed = 0
    for collection_id, edit in updates.items():
        if edit.get("_delete"):
            if collection_id in current:
                remove_collection_lot(conn, collection_id)
                changed += 1
            continue
        old = current.get(collection_id, {})
        new_vals = {k: edit.get(k) for k in COLLECTION_FIELDS if k in edit}
        if "foil" in new_vals:
            new_vals["foil"] = 1 if new_vals["foil"] else 0
        if any(old.get(k) != v for k, v in new_vals.items()):
            update_collection_lot(conn, collection_id, **new_vals)
            changed += 1
    return changed


# ------------------------------------------------------------------
# Deck Swap / Upgrade Manager (Prompt Pass 13 / prompt6.txt) — applies
# one queued swap from the Deck Editor's Swap Manager tab. Card resolution
# (name/set/number -> scryfall_id, including a live Scryfall lookup for
# a brand-new card) happens at the call site via card_resolver, same
# division of responsibility as the Mainboard tab's own "Add a card"
# flow — this function only takes already-resolved printings and does
# the actual deck/collection writes.
# ------------------------------------------------------------------
def execute_swap(conn, deck_id, deck_name, remove_scryfall_id, remove_card_name,
                  add_scryfall_id, add_card_name, quantity=1, default_location=None):
    """Removes `remove_scryfall_id` from the deck's mainboard and adds
    `add_scryfall_id` in its place at `quantity` copies, then reconciles
    collection.location for both cards so the collection doesn't quietly
    drift out of sync with what the deck actually runs:

      - any collection lot for the removed card currently sleeved in
        THIS deck (Location == deck_name) moves back to
        `default_location` (DEFAULT_LOCATION/"Box" if not given) — same
        "return to storage" convention delete_deck() uses.
      - the added card gets sleeved here: reuses the first available
        (not already sleeved in any deck) lot for it if one exists
        (queries.card_inventory_status()), else creates a brand-new
        starter lot (source "Auto-added (swap manager)"). Unlike
        auto_add_to_collection(), this isn't gated on "zero lots exist
        anywhere" — a card that's owned but every copy is already
        sleeved in some OTHER deck still needs its own fresh lot here
        rather than silently stealing one from that other deck's box.

    Returns {"moved_out": bool, "assigned_lot_id": int_or_None,
    "created_lot": bool} describing what happened on the collection
    side, so the caller can report it per swap."""
    default_location = default_location or DEFAULT_LOCATION

    _delete_deck_card(conn, deck_id, remove_scryfall_id)
    _put_deck_card(conn, deck_id, add_scryfall_id, quantity)

    moved_out = False
    if remove_card_name:
        rows = conn.execute(
            """SELECT col.collection_id FROM collection col
               JOIN cards c ON c.scryfall_id = col.scryfall_id
               WHERE c.name = ? COLLATE NOCASE AND col.location = ?""",
            (remove_card_name, deck_name),
        ).fetchall()
        for (collection_id,) in rows:
            update_collection_lot(conn, collection_id, location=default_location)
            moved_out = True

    assigned_lot_id = None
    created_lot = False
    status = q.card_inventory_status(conn, add_card_name, deck_name=deck_name)
    if status["available_lots"]:
        assigned_lot_id = status["available_lots"][0]["collection_id"]
        update_collection_lot(conn, assigned_lot_id, location=deck_name)
    elif status["in_this_deck_qty"] == 0:
        assigned_lot_id = add_collection_lot(
            conn, add_scryfall_id,
            quantity=quantity, foil=False, location=deck_name,
            date_acquired=datetime.date.today().isoformat(), price_paid=None,
            source="Auto-added (swap manager)",
        )
        created_lot = True

    return {"moved_out": moved_out, "assigned_lot_id": assigned_lot_id, "created_lot": created_lot}



# ------------------------------------------------------------------
# Deck lifecycle: brewing + dismantling (Phase 6)
#
# The two operations the app had no concept of: building a deck whose cards
# are still elsewhere, and taking a deck apart to feed another one.
#
# Neither needs a new table. A dismantle is a BATCH OF deck_changes ROWS —
# removals against the source, adds against the target — and that is what
# buys all of the following for free: the Phase 2 allocator already refuses
# to double-promise a copy, the batch stages as 'planned' and previews
# before anything is written like every other change, and it lands in both
# decks' Phase 5 history automatically. That is the payoff of the
# one-lifecycle-table decision from Phase 0.
#
# delete_deck() keeps its current meaning for a deck that is genuinely
# gone. Dismantling is the other path: the deck ends at
# build_state='dismantled' with its list and history intact, so you can see
# what it was and rebuild it later. Deleting destroys the record;
# dismantling keeps it.
# ------------------------------------------------------------------
def set_build_state(conn, deck_id, state):
    """Set (or clear, with state=None) a deck's build_state. Returns the
    deck's name; raises ValueError for an unknown deck or state.

    'brewing' is what makes a deck's Build Plan appear, and clearing it back
    to NULL is the "Mark deck as built" action. Build *completion* is never
    written here — it is derived from where the cards physically are (see
    queries.deck_sourcing_plan), so it cannot go stale."""
    if state is not None and state not in q.BUILD_STATES:
        raise ValueError(f"Unknown build state {state!r}.")
    row = conn.execute("SELECT name FROM decks WHERE deck_id=?", (deck_id,)).fetchone()
    if row is None:
        raise ValueError("Deck not found.")
    conn.execute("UPDATE decks SET build_state=? WHERE deck_id=?", (state, deck_id))
    conn.commit()
    logger.info("Deck %r (deck_id=%d) build_state -> %r", row[0], deck_id, state)
    return row[0]


def stage_dismantle(conn, deck_id, target_deck_id=None, transfer_oracle_ids=None,
                    box_oracle_ids=None, dest_location=None, notes=None):
    """Stage taking a deck apart as planned deck_changes rows. Writes no
    mainboard or collection change of its own — everything it creates is
    'planned' and goes through the normal preview-then-apply path, so
    nothing physical happens until the batch is applied.

    `transfer_oracle_ids` / `box_oracle_ids` select which of
    queries.deck_dismantle_plan()'s rows to stage, by oracle_id; None means
    "all of that bucket". An explicit empty collection stages none of it,
    which is how "leave this card in the deck for now" is expressed.

    For each transferred card, TWO rows are written:

      - a removal against the source, carrying dest_location = the target
        deck's name, so applying it moves the physical lot source → target
        in one step with no stop in the box
      - an addition against the target

    The removal is written first so that change_id order — the order
    apply_changes() walks a batch in — applies the cut before the add.
    Reversed, the add would try to source a copy still sleeved in the deck
    being taken apart, and would create a redundant lot for it.

    Each row names the printing belonging to ITS OWN deck's list
    (source_scryfall_id / target_scryfall_id), not the sleeved lot's — see
    deck_dismantle_plan() on why those three can differ.

    Cards going to storage get a removal only, with dest_location set to
    `dest_location` (default DEFAULT_LOCATION, "Box").

    Returns {"removed": [change_id, ...], "added": [change_id, ...],
    "skipped": [(card, reason), ...]}. A card is skipped rather than staged
    when the target already has an open change adding it, or when the
    source already has one cutting it — the same duplicate guard
    add_change() applies everywhere else, kept here so staging a deck twice
    can't queue two cuts of one card or two claims on one copy.

    The deck's build_state is NOT set here: it becomes 'dismantled' when the
    batch is applied, not when it is planned, because a staged dismantle you
    never apply shouldn't relabel the deck."""
    plan = q.deck_dismantle_plan(conn, deck_id, target_deck_id)
    if plan is None:
        raise ValueError("Deck not found.")
    dest_location = dest_location or DEFAULT_LOCATION
    target_name = plan["target_deck_name"]

    def wanted(rows, allow):
        if allow is None:
            return rows
        allow = set(allow)
        return [r for r in rows if r["oracle_id"] in allow]

    transfers = wanted(plan["direct_transfer"], transfer_oracle_ids) if target_name else []
    to_box = wanted(plan["to_box"], box_oracle_ids)
    result = {"removed": [], "added": [], "skipped": []}

    def removal(card, dest):
        if _open_removal_for_card(conn, deck_id, card["source_scryfall_id"]):
            result["skipped"].append(
                (card["card"], f"already queued to leave {plan['deck_name']}")
            )
            return False
        cur = conn.execute(
            """INSERT INTO deck_changes
                   (deck_id, status, remove_scryfall_id, remove_name, quantity,
                    notes, planned_at, dest_location)
               VALUES (?, 'planned', ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)""",
            (deck_id, card["source_scryfall_id"], card["card"], card["copies"],
             notes or f"Dismantle {plan['deck_name']}", dest),
        )
        result["removed"].append(cur.lastrowid)
        return True

    for card in transfers:
        if not card["source_scryfall_id"] or not card["target_scryfall_id"]:
            # Nothing to cut, or nothing on the target's list to add to.
            result["skipped"].append((card["card"], "not on both decks' lists"))
            continue
        if _open_change_for_card(conn, plan["target_deck_id"], card["target_scryfall_id"]):
            result["skipped"].append((card["card"], f"already on {target_name}'s shortlist"))
            continue
        if not removal(card, target_name):
            continue
        cur = conn.execute(
            """INSERT INTO deck_changes
                   (deck_id, status, add_scryfall_id, add_name, quantity,
                    notes, planned_at)
               VALUES (?, 'planned', ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (plan["target_deck_id"], card["target_scryfall_id"], card["card"],
             card["copies"], notes or f"From {plan['deck_name']}"),
        )
        result["added"].append(cur.lastrowid)

    for card in to_box:
        if not card["source_scryfall_id"]:
            result["skipped"].append((card["card"], "not on this deck's list"))
            continue
        removal(card, dest_location)

    conn.commit()
    logger.info(
        "Staged dismantle of deck_id=%d: %d removals, %d adds to %r, %d skipped",
        deck_id, len(result["removed"]), len(result["added"]), target_name,
        len(result["skipped"]),
    )
    return result


def move_dismantle_strays(conn, deck_id, dest_location=None, collection_ids=None):
    """Move lots sleeved in a deck whose card is NOT on its list out to
    `dest_location` (default "Box"). Returns the number of lots moved.

    These are the leftovers of past swaps — real cards in the sleeves with
    no list entry behind them — so they are not deck_changes material: there
    is no list change to record, only a physical one. Keeping them out of
    the staged batch is deliberate, so a dismantle's planned rows stay a
    faithful description of what happens to the DECK, and these are reported
    and moved beside it instead.

    `collection_ids` restricts the move to those lots; None moves every
    stray. Phase 4's Reconcile "Put away" does the same job one deck at a
    time and remains the place to deal with strays outside a dismantle."""
    plan = q.deck_dismantle_plan(conn, deck_id)
    if plan is None:
        raise ValueError("Deck not found.")
    dest_location = dest_location or DEFAULT_LOCATION
    allow = None if collection_ids is None else set(collection_ids)
    moved = 0
    for card in plan["strays"]:
        for lot in card["lots"]:
            if allow is not None and lot["collection_id"] not in allow:
                continue
            move_lot(conn, lot["collection_id"], dest_location)
            moved += 1
    logger.info("Moved %d stray lot(s) out of deck_id=%d to %r", moved, deck_id, dest_location)
    return moved

# ------------------------------------------------------------------
# Commander Spellbook combo cache (deck_combos / deck_combo_sync).
# dashboard_lib/spellbook.py does the fetching and normalizing; these two
# functions are the only things that write the cache. See schema.sql for
# the table design and the newline-separated-list convention.
# ------------------------------------------------------------------
def _join_list(values):
    """Serialize a combo's card/feature list for storage. Newline-separated
    because card names contain commas but never newlines — see schema.sql."""
    return "\n".join(v for v in (values or []) if v)


def save_deck_combos(conn, deck_id, result):
    """Replace this deck's cached combos with a fresh
    spellbook.normalize_response() result, and stamp deck_combo_sync.

    Replace-not-merge is deliberate: a combo that no longer shows up
    (because a card left the deck, or Spellbook retired the variant) has to
    disappear, and a stale row that merging would preserve is exactly the
    wrong answer for a cache whose whole job is to reflect the current
    decklist. Both tables are rewritten in one transaction so an
    interrupted refresh can't leave a sync stamp claiming combos that
    aren't there.

    Returns (included_count, almost_count).
    """
    included = result.get("included") or []
    almost = result.get("almost") or []

    conn.execute("DELETE FROM deck_combos WHERE deck_id = ?", (deck_id,))

    rows = []
    for category, combos in (("included", included), ("almost", almost)):
        for combo in combos:
            rows.append((
                deck_id,
                combo["combo_id"],
                category,
                _join_list(combo.get("uses")),
                _join_list(combo.get("requires")),
                _join_list(combo.get("missing")),
                _join_list(combo.get("produces")),
                combo.get("description") or "",
                combo.get("prerequisites") or "",
                combo.get("mana_needed") or "",
                combo.get("popularity"),
                combo.get("bracket_tag"),
                1 if combo.get("commander_legal") else 0,
            ))

    if rows:
        # INSERT OR REPLACE rather than plain INSERT: a single deck can
        # legitimately be handed the same combo_id twice by the API (the
        # same variant surfacing in more than one response bucket), and the
        # PK would otherwise abort the whole refresh over a harmless dupe.
        conn.executemany(
            """INSERT OR REPLACE INTO deck_combos
                   (deck_id, combo_id, category, uses, requires, missing, produces,
                    description, prerequisites, mana_needed, popularity, bracket_tag,
                    commander_legal)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )

    conn.execute(
        """INSERT OR REPLACE INTO deck_combo_sync
               (deck_id, fetched_at, identity, included_count, almost_count)
           VALUES (?,?,?,?,?)""",
        (
            deck_id,
            datetime.datetime.now().isoformat(timespec="seconds"),
            result.get("identity") or "",
            len(included),
            len(almost),
        ),
    )
    conn.commit()
    return len(included), len(almost)


def clear_deck_combos(conn, deck_id):
    """Drop a deck's cached combos AND its sync stamp, putting it back to
    the "never fetched" state (rather than the "fetched, found nothing"
    state that deleting only deck_combos rows would leave behind)."""
    conn.execute("DELETE FROM deck_combos WHERE deck_id = ?", (deck_id,))
    conn.execute("DELETE FROM deck_combo_sync WHERE deck_id = ?", (deck_id,))
    conn.commit()

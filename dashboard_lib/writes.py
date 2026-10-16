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
import os
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
# Reference-list maintenance (UI plan Phase 8) — remove names from a tag
# list outright, and back the lists up as the CSVs migrate.py reads.
# ------------------------------------------------------------------
def delete_game_changer_names(conn, names):
    """Remove every game_changer_tags row for each name (a stale list entry
    dropped on purpose). Returns how many names had rows."""
    return _delete_tag_list_names(conn, "game_changer_tags", names)


def delete_mana_tag_names(conn, names):
    """Remove every mana_tags row for each name. Returns how many names had rows."""
    return _delete_tag_list_names(conn, "mana_tags", names)


def _delete_tag_list_names(conn, table, names):
    removed = 0
    for name in names:
        cur = conn.execute(f"DELETE FROM {table} WHERE card_name=?", (name,))
        removed += 1 if cur.rowcount else 0
    conn.commit()
    return removed


# Column headers match what migrate.py's load_static_reference_lists reads.
# utf-8-sig + CRLF mirrors the hand-made files already in data/.
REFERENCE_LIST_EXPORTS = [
    ("game_changers.csv", "game_changer_tags", "Game Changers"),
    ("mana_tags.csv", "mana_tags", "Optimized Mana"),
]


def export_reference_lists(conn, data_dir):
    """Write game_changers.csv / mana_tags.csv into `data_dir` from the
    live tag tables, so a migrate.py rebuild can't discard hand-curated
    work. Returns {filename: row_count}."""
    import csv
    counts = {}
    for filename, table, header in REFERENCE_LIST_EXPORTS:
        rows = conn.execute(
            f"SELECT card_name, tag FROM {table} ORDER BY card_name COLLATE NOCASE, tag COLLATE NOCASE"
        ).fetchall()
        with open(os.path.join(data_dir, filename), "w", encoding="utf-8-sig", newline="") as f:
            out = csv.writer(f, lineterminator="\r\n")
            out.writerow([header, "Tag"])
            out.writerows([(r[0], r[1]) for r in rows])
        counts[filename] = len(rows)
    return counts


# ------------------------------------------------------------------
# Orphan cleanup (UI plan Phase 8). Each delete re-checks the reference
# rules itself rather than trusting the ids it is handed, so a stale
# report can never remove something that has since become used.
# ------------------------------------------------------------------
def delete_orphan_cards(conn, scryfall_ids):
    """Delete `cards` rows that nothing references at all (see
    queries.unreferenced_card_sql); any id that is referenced is skipped.
    Returns the list of (scryfall_id, name) actually deleted."""
    ok = {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT c.scryfall_id, c.name FROM cards c WHERE {q.unreferenced_card_sql(conn)}"
        )
    }
    deleted = [(sid, ok[sid]) for sid in scryfall_ids if sid in ok]
    for sid, _ in deleted:
        conn.execute("DELETE FROM cards WHERE scryfall_id=?", (sid,))
    conn.commit()
    if deleted:
        logger.info("Deleted %d unreferenced card row(s): %s", len(deleted), ", ".join(n for _, n in deleted))
    return deleted


def delete_orphan_card_tags(conn, keys):
    """Delete card_tags rows for a card that is no longer in that deck.
    `keys` is [(deck_id, scryfall_id), ...]; a pair whose card is still in
    the deck is skipped. Returns the number of tag rows deleted."""
    deleted = 0
    for deck_id, sid in keys:
        cur = conn.execute(
            """DELETE FROM card_tags WHERE deck_id=? AND scryfall_id=?
               AND NOT EXISTS (SELECT 1 FROM deck_cards dc
                               WHERE dc.deck_id=card_tags.deck_id AND dc.scryfall_id=card_tags.scryfall_id)""",
            (deck_id, sid),
        )
        deleted += cur.rowcount
    conn.commit()
    return deleted


def delete_orphan_image_files(conn, filenames, image_dir):
    """Delete image-cache files that have no `cards` row. Only plain files
    directly inside `image_dir` whose stem is not a known scryfall_id are
    removed. Returns the filenames deleted."""
    known = {r[0] for r in conn.execute("SELECT scryfall_id FROM cards")}
    deleted = []
    for filename in filenames:
        path = os.path.join(image_dir, os.path.basename(filename))
        if os.path.isfile(path) and os.path.splitext(os.path.basename(filename))[0] not in known:
            os.remove(path)
            deleted.append(filename)
    return deleted


def clear_missing_image_paths(conn, scryfall_ids):
    """Null local_image_path on cards whose cached file no longer exists, so
    they fall back to the remote image (and the next sync re-downloads)."""
    for sid in scryfall_ids:
        conn.execute("UPDATE cards SET local_image_path = NULL WHERE scryfall_id=?", (sid,))
    conn.commit()
    return len(scryfall_ids)


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
    """relative_path is expected to already be relative to static/
    (e.g. 'image_cache/deck_covers/12.png'), matching how
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


def _cascade_deck_name(conn, old_name, new_name, update_collection_locations=True):
    """Keep every place a deck's name is stored as free text in sync on a
    rename (rule 3 of EDIT_FLOW_PLAN.md): `collection.location` (what
    "sleeved in this deck" tracking matches on) and `deck_changes.dest_location`
    on **open** (idea/planned) rows — a dismantle transfer or any other
    planned removal that names this deck as where the copy is headed.
    Applied rows are left alone: they're history, recording what the
    target was called at the time, not what it's called now. A new
    free-text deck-name column added anywhere gets a line here too.

    `source_location` (Phase 2) holds a deck's name only when source='deck'
    (Phase 3's take-from-another-deck); for source='storage' it is a storage
    Location, which a deck rename doesn't touch — hence the source guard."""
    if update_collection_locations:
        conn.execute("UPDATE collection SET location=? WHERE location=?", (new_name, old_name))
    conn.execute(
        """UPDATE deck_changes SET dest_location=?
           WHERE dest_location=? AND status IN ('idea','planned')""",
        (new_name, old_name),
    )
    conn.execute(
        """UPDATE deck_changes SET source_location=?
           WHERE source_location=? AND source='deck' AND status IN ('idea','planned')""",
        (new_name, old_name),
    )


def rename_deck(conn, deck_id, new_name, update_collection_locations=True):
    """Returns (success, error). decks.name is UNIQUE and is matched as
    free text elsewhere (collection.location for "sleeved in this deck"
    tracking, deck_changes.dest_location for an open removal headed to
    this deck, and scripts/migrate.py's tag backup/restore keys off the
    name too) — so a rename here, by default, also updates any
    collection.location rows that exactly matched the old name (open
    dest_location rows are always kept in sync, since they aren't
    collection data). It does NOT touch decks.csv/deck_mapping.csv: if you
    later re-run the CSV migration, the deck will reappear under its old
    CSV name unless you update the CSV to match."""
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
    _cascade_deck_name(conn, old_name, new_name, update_collection_locations)
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
    Returns (deck_name, error, redirected, dropped) — error is None on
    success; redirected is the number of OTHER decks' open deck_changes rows
    whose dest_location named this deck (e.g. an old, unlinked dismantle
    transfer feeding it) that got reassigned to DEFAULT_LOCATION, since that
    target no longer exists to receive them. Applied rows are left alone —
    they're history.

    `dropped` (EDIT_FLOW_PLAN.md Phase 3) is the number of other decks' open
    rows dropped because they were half of a take with this deck — a take
    from a deck that no longer exists can't be applied, nor can a promise to
    one. linked_change_id is cleared on every other-deck row (applied ones
    too) that pointed at this deck's rows, so no reference dangles; an
    unlinked open row still naming this deck as its source goes back to
    Auto.

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
        return None, "Deck not found.", 0, 0
    deck_name = row[0]

    conn.execute(
        "UPDATE game_participants SET deck_id=NULL, is_own_deck=0 WHERE deck_id=?", (deck_id,)
    )

    # Phase 3: the other halves of this deck's takes. Before the redirect
    # below, so a dropped promise isn't also counted as redirected.
    partners = conn.execute(
        """SELECT p.change_id, p.status FROM deck_changes p
           JOIN deck_changes mine ON mine.change_id = p.linked_change_id
           WHERE mine.deck_id = ? AND p.deck_id != ?""",
        (deck_id, deck_id),
    ).fetchall()
    dropped = 0
    for change_id, status in partners:
        if status in OPEN_CHANGE_STATUSES:
            _set_dropped(conn, change_id, f"{TAKE_DROPPED_NOTE}: {deck_name} was deleted")
            dropped += 1
        conn.execute("UPDATE deck_changes SET linked_change_id=NULL WHERE change_id=?", (change_id,))
    conn.execute(
        """UPDATE deck_changes SET source=NULL, source_location=NULL
           WHERE source='deck' AND source_location=? AND status IN ('idea','planned') AND deck_id != ?""",
        (deck_name, deck_id),
    )

    # Other decks' open rows pointed at this deck as a removal's
    # destination (e.g. a dismantle transfer feeding it) can't be applied
    # to a target that no longer exists — fall back to the same
    # DEFAULT_LOCATION a dest_location of NULL already means.
    redirected = conn.execute(
        """SELECT COUNT(*) FROM deck_changes
           WHERE dest_location=? AND status IN ('idea','planned') AND deck_id != ?""",
        (deck_name, deck_id),
    ).fetchone()[0]
    conn.execute(
        """UPDATE deck_changes SET dest_location=?
           WHERE dest_location=? AND status IN ('idea','planned') AND deck_id != ?""",
        (DEFAULT_LOCATION, deck_name, deck_id),
    )

    for table in _DECK_SCOPED_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE deck_id=?", (deck_id,))

    if clear_collection_locations:
        # Phase 6: each sleeved lot's trip to storage goes in the move log
        # (rename_deck() logs nothing — a relabel moves no copy).
        for collection_id, sid, qty in conn.execute(
            "SELECT collection_id, scryfall_id, quantity FROM collection WHERE location=?",
            (deck_name,),
        ).fetchall():
            _log_collection_move(conn, collection_id, sid, qty, deck_name, DEFAULT_LOCATION,
                                 "deck_delete")
        conn.execute(
            "UPDATE collection SET location=? WHERE location=?", (DEFAULT_LOCATION, deck_name)
        )

    conn.execute("DELETE FROM decks WHERE deck_id=?", (deck_id,))
    conn.commit()
    logger.info("Deleted deck %r (deck_id=%d); redirected %d open row(s) pointed at it, dropped %d "
                "linked row(s)", deck_name, deck_id, redirected, dropped)
    return deck_name, None, redirected, dropped


# ------------------------------------------------------------------
# Mainboard (deck_cards)
#
# add_deck_card / remove_deck_card / bulk_update_deck_cards are the
# hand-edit paths (the Board, the add bar, the Mainboard table). Each one
# records an 'applied' row in deck_changes for every change to the list —
# a join, a removal, or (Phase 0) a quantity change logged as the
# difference — so a deck's history covers more than swaps. The
# underscore-prefixed _put/_delete helpers do the same write WITHOUT
# logging, for execute_swap(), whose caller (apply_change) records the
# whole swap as ONE row instead of an add plus a remove.
#
# EDIT_FLOW_PLAN.md Phase 1: each of them also moves the physical copies
# (sleeve_into_deck / unsleeve_from_deck, below) and returns the list of
# moves, so the caller can show them and the Board can undo them.
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
        _set_dropped(conn, change_id, AUTO_DROP_NOTE)
        # A take from another deck (Phase 3) goes as a pair: the donor's
        # promised removal has nothing left to feed.
        partner = _open_partner(conn, get_change(conn, change_id))
        if partner:
            _set_dropped(conn, partner["change_id"], TAKE_DROPPED_NOTE)
    if change_ids:
        conn.commit()
        logger.info("Auto-dropped %d stale shortlist row(s) on deck %s: %s",
                    len(change_ids), deck_id, change_ids)
    return change_ids


def add_deck_card(conn, deck_id, scryfall_id, quantity=1, with_moves=True, note=None,
                  source=None, source_location=None, dest=None):
    """Set a card's quantity on the deck's list (adding it if absent), log
    the change, and move copies to match. Returns the moves made.

    `with_moves=False` changes the list and history only — the Board's Undo
    uses it, then puts each copy back with revert_moves(), which re-running
    the Auto rule can't promise now that an edit can name its source.
    `note` goes on the history row (Phase 6: a Collection-page edit that
    the deck's list followed says so).

    `source` / `source_location` (Phase 2) say where an INCREASE comes from:
    None is Auto, ("storage", "Lands Box") takes the new copies from that
    Location only, and is refused before anything is written if that
    Location doesn't hold enough free copies (a brewing deck's list isn't
    checked: no copy moves there). They go on the history row, so History
    can say where it came from. `dest` is where a DECREASE puts the copies
    away (default Box), logged the same way. Each applies only in its own
    direction and is ignored in the other.

    ("buy", None) (Phase 4) puts the increase on the list with NO copy moved
    or made — no phantom lot — and the history row says `buy`, which is what
    puts the card on the Workbench buy list until a copy is sleeved there.

    ("deck", "Gisa") (Phase 3) takes the increase out of Gisa: its list gives
    the card up too, through transfer_between_decks(), which this hands the
    increase to — see there for what it checks and writes."""
    source, source_location = normalize_source(conn, source, source_location, deck_id=deck_id,
                                               scryfall_id=scryfall_id)
    dest = normalize_dest(conn, dest, scryfall_id=scryfall_id)
    existing = _deck_card_quantity(conn, deck_id, scryfall_id)
    growing = existing is None or quantity > existing
    if not growing:
        source, source_location = None, None
    if growing or quantity == existing:
        dest = None
    if source == "deck":
        return transfer_between_decks(conn, _deck_id_by_name(conn, source_location), deck_id,
                                      scryfall_id, quantity - (existing or 0), note=note)["moves"]
    if source == "storage" and with_moves:
        _check_stock(conn, deck_id, scryfall_id,
                     q.deck_list_quantity(conn, deck_id, scryfall_id) - (existing or 0) + quantity,
                     source_location)
    _put_deck_card(conn, deck_id, scryfall_id, quantity)
    change_id = None
    if existing is None:
        change_id = _log_applied_change(conn, deck_id, add_scryfall_id=scryfall_id,
                                        quantity=quantity, notes=note,
                                        source=source, source_location=source_location)
        # Only on a genuine join: a quantity bump doesn't retire anything,
        # and execute_swap() goes through _put_deck_card, so applying a
        # planned swap never auto-drops its own row.
        _auto_drop_stale_ideas(conn, deck_id, scryfall_id)
    elif quantity != existing:
        # Phase 0: a bump (Swamp x4 -> x5) is logged too, as the
        # difference rather than a second "add" of the whole new count —
        # it isn't a join (no auto-drop) and History tells it apart from a
        # real add/remove by its notes rather than by a separate status.
        notes = f"quantity {existing} → {quantity}" + (f" ({note})" if note else "")
        if quantity > existing:
            change_id = _log_applied_change(conn, deck_id, add_scryfall_id=scryfall_id,
                                            quantity=quantity - existing, notes=notes,
                                            source=source, source_location=source_location)
        else:
            change_id = _log_applied_change(conn, deck_id, remove_scryfall_id=scryfall_id,
                                            quantity=existing - quantity, notes=notes,
                                            dest_location=dest)
    if not with_moves:
        return []
    # Only the difference moves: +1 sleeves one more copy, -1 puts one away.
    if growing:
        return sleeve_into_deck(conn, deck_id, scryfall_id, quantity - (existing or 0),
                                source=source, source_location=source_location,
                                change_id=change_id)
    if quantity < existing:
        return unsleeve_from_deck(conn, deck_id, scryfall_id, existing - quantity,
                                  dest=dest, change_id=change_id)
    return []


def remove_deck_card(conn, deck_id, scryfall_id, with_moves=True, note=None, dest=None):
    """Take a card off the deck's list, log it, and put its sleeved copies
    away (to `dest`, a storage Location — DEFAULT_LOCATION when None; the
    choice goes on the history row). Returns the moves made; `with_moves`
    and `note` as for add_deck_card()."""
    dest = normalize_dest(conn, dest, scryfall_id=scryfall_id)
    existing = _deck_card_quantity(conn, deck_id, scryfall_id)
    _delete_deck_card(conn, deck_id, scryfall_id)
    if existing is None:
        return []
    change_id = _log_applied_change(conn, deck_id, remove_scryfall_id=scryfall_id,
                                    quantity=existing, notes=note, dest_location=dest)
    _drop_promises(conn, deck_id, scryfall_id)
    return (unsleeve_from_deck(conn, deck_id, scryfall_id, existing, dest=dest, change_id=change_id)
            if with_moves else [])


def bulk_update_deck_cards(conn, deck_id, updates, with_moves=True, dest=None):
    """updates: {scryfall_id: new_quantity} — a card mapped to None or a
    non-positive quantity is removed instead of set. Returns (changed,
    moves): the number of cards actually changed, and every copy moved
    (add_deck_card / remove_deck_card's, in order). `dest` is where every
    copy this save puts away goes (Phase 2's "Removed copies go to"); None
    is Box."""
    dest = normalize_dest(conn, dest)
    current = dict(
        conn.execute("SELECT scryfall_id, quantity FROM deck_cards WHERE deck_id=?", (deck_id,)).fetchall()
    )
    changed, moves = 0, []
    for scryfall_id, qty in updates.items():
        old_qty = current.get(scryfall_id)
        if qty is None or qty <= 0:
            if old_qty is not None:
                moves += remove_deck_card(conn, deck_id, scryfall_id, with_moves=with_moves, dest=dest)
                changed += 1
            continue
        if qty != old_qty:
            moves += add_deck_card(conn, deck_id, scryfall_id, qty, with_moves=with_moves, dest=dest)
            changed += 1
    return changed, moves


# ------------------------------------------------------------------
# Moves (EDIT_FLOW_PLAN.md Phase 1) — the one place a deck edit decides
# what happens to physical copies, whichever page it started on.
#
# A move is a plain dict:
#
#   card           card name, for display
#   quantity       copies (None for an untracked lot moved whole)
#   collection_id  the lot now at `to` (a split leaves the rest behind
#                  under the old id), or the starter lot made / deleted
#   from, to       Locations as stored (a deck name, a storage box, or
#                  None for "no location recorded")
#   kind           what happened, which is also how it reads:
#                    move        a lot moved from -> to
#                    new         a starter lot made at `to` (a "new copy
#                                record"; from is None)
#                    delete      a starter lot deleted again by Undo
#                    bulk        a basic land (Phase 7) — no lot is ever
#                                touched; from/to are the deck and None
#                                ("basic land supply"), `direction` in/out,
#                                and the step is still in the move log
#                    brewing     the deck is brewing (rule 4) — list only;
#                                `direction` says whether copies would have
#                                come in or gone out
#                    dismantled  likewise for a dismantled deck (in only)
#                    buy         a planned purchase (Phase 4): the list has
#                                the card, no lot exists to move or make
#
# `kind` rather than a sentinel in from/to, because "Bulk" or "Brewing"
# could be someone's real storage box name.
# ------------------------------------------------------------------
STARTER_LOT_SOURCE = "Auto-added (deck edit)"


# ------------------------------------------------------------------
# Choosing where a copy comes from / goes (EDIT_FLOW_PLAN.md Phase 2).
# Everything that takes a source or a dest goes through these two, so a bad
# value (a typo from the browser, a deck name, a source that doesn't exist
# yet) is refused with a reason before a list or a lot is touched.
# ------------------------------------------------------------------
def normalize_source(conn, source, source_location, deck_id=None, scryfall_id=None):
    """(source, source_location) as stored: (None, None) for Auto (None, "",
    "auto", or a Location with no source), ("storage", <Location in the
    catalog's spelling>), or (Phase 3) ("deck", <another deck's exact name>).
    (Phase 4) ("buy", None): a planned purchase, which has no Location — any
    `source_location` passed with it is dropped.
    ValueError for a source that doesn't exist, a pick
    with no Location / deck, a deck name given as a storage Location, an
    unknown deck, or — with `deck_id`, the deck receiving the card — a deck
    taking a card from itself.

    With `scryfall_id` naming a basic land (Phase 7) any value is IGNORED
    rather than refused — a basic has nowhere to come from — so a bulk save
    across mixed cards doesn't fail on its basics."""
    if scryfall_id and q.is_basic_land(conn, scryfall_id=scryfall_id):
        return None, None
    if source in (None, "", "auto"):
        return None, None
    if source not in q.SOURCES:
        raise ValueError(
            f"Taking a card from {source!r} isn't a choice — leave it on Auto, or pick a storage "
            "Location, another deck or Buy."
        )
    if source == "buy":
        return "buy", None
    place = (source_location or "").strip()
    if source == "deck":
        if not place:
            raise ValueError("Pick which deck it comes from.")
        if place not in q.deck_name_set(conn):
            raise ValueError(f"There's no deck called {place!r} to take it from.")
        if deck_id is not None and _deck_name_and_state(conn, deck_id)[0] == place:
            raise ValueError("A deck can't take a card from itself.")
        return "deck", place
    if not place:
        raise ValueError("Pick which storage Location it comes from.")
    if place in q.deck_name_set(conn):
        raise ValueError(f"{place} is a deck, not a storage Location — choose it as the deck the card "
                         "comes from instead.")
    return "storage", next((known for known in q.storage_locations(conn)
                            if q.same_location(known, place)), place)


def normalize_dest(conn, dest, scryfall_id=None):
    """A removal's destination as stored: None for the default, else a known
    storage Location in the catalog's spelling. ValueError for a deck name
    (a copy goes to another deck only as a take, which that deck asks for —
    Phase 3 — so both lists change together) or a Location nobody has set
    up, so a typo can't strand a copy somewhere that isn't on any list.
    Ignored (None) for a basic land, like normalize_source()."""
    if scryfall_id and q.is_basic_land(conn, scryfall_id=scryfall_id):
        return None
    place = (dest or "").strip()
    if not place:
        return None
    if place in q.deck_name_set(conn):
        raise ValueError(f"{place} is a deck, not a storage Location — to hand the card to {place}, "
                         f"take it from this deck on {place}'s side instead.")
    for known in q.storage_locations(conn):
        if q.same_location(known, place):
            return known
    raise ValueError(f"{place!r} isn't one of your storage Locations — add it under Collection Editor first.")


def _check_stock(conn, deck_id, scryfall_id, list_after, location):
    """Refuse, before anything is written, a chosen storage Location that
    can't supply the copies a list change needs: `list_after` is how many
    the list will run, minus copies already sleeved here, must come from
    `location`. Nothing to check for a brewing / dismantled deck (list
    only) or a basic land (Phase 7: never moved)."""
    _name, state = _deck_name_and_state(conn, deck_id)
    if state in q.BUILD_STATES or q.is_basic_land(conn, scryfall_id=scryfall_id):
        return
    deck_name = _name
    here = sum(qty for _, qty, loc, _ in q.card_lots(conn, scryfall_id) if (loc or "").strip() == deck_name)
    need = list_after - here
    if need <= 0:
        return
    have = sum(lot[1] for lot in q.free_lots(conn, scryfall_id, location=location))
    if have < need:
        card = _card_name(conn, scryfall_id) or scryfall_id
        raise ValueError(
            f"{card}: {location} has {have} free cop{'y' if have == 1 else 'ies'}, {need} needed. "
            "Pick another Location, or leave it on Auto."
        )
# Kinds that record a decision but touch no lot; revert_moves() skips them.
PLACEHOLDER_MOVE_KINDS = ("bulk", "brewing", "dismantled", "buy")


def _move(card, quantity, kind, collection_id=None, frm=None, to=None, **extra):
    return {"card": card, "quantity": quantity, "collection_id": collection_id,
            "from": frm, "to": to, "kind": kind, **extra}


def _bulk_move(conn, deck_name, scryfall_id, card, count, direction, reason="deck_edit",
               change_id=None):
    """A basic land joining (`direction` "in") or leaving ("out") a deck's
    list (EDIT_FLOW_PLAN.md Phase 7). Basics are generic: no lot is touched,
    even when you track one. The step is still logged in collection_moves
    with bulk=1 and no collection_id — "basic land supply → Prosper" — so Card
    Lookup shows every basic added or removed. Returns the `bulk` move."""
    frm, to = (None, deck_name) if direction == "in" else (deck_name, None)
    _log_collection_move(conn, None, scryfall_id, count, frm, to, reason, change_id, bulk=True)
    conn.commit()
    return _move(card, count, "bulk", frm=frm, to=to, direction=direction, scryfall_id=scryfall_id)


def _deck_name_and_state(conn, deck_id):
    row = conn.execute("SELECT name, build_state FROM decks WHERE deck_id=?", (deck_id,)).fetchone()
    if row is None:
        raise ValueError("Deck not found.")
    return row[0], row[1]


def sleeve_into_deck(conn, deck_id, scryfall_id, quantity, source=None, source_location=None,
                     force=False, reason="deck_edit", change_id=None):
    """Put up to `quantity` more copies of a card into a deck's sleeves and
    return the moves made. Call it AFTER the list change: it never sleeves
    more than the list now runs.

    `source=None` is "Auto", the rule execute_swap() always used:

      1. copies already sleeved here count first — a stray left from an
         old swap is adopted, and a direct transfer whose removal side
         already moved the lot in finds nothing left to do
      2. then free copies in storage (sleeved in no deck, unlocated
         included), biggest lot first, splitting a lot with move_lot() —
         the order plan_availability() allocates in, so the copy the
         Workbench promised is the one that moves
      3. then a starter lot for the remainder (STARTER_LOT_SOURCE), which
         can record a copy you don't own yet (EDIT_FLOW_PLAN.md open
         question 3)

    `source="storage"` with a `source_location` (Phase 2) takes copies from
    that Location only, biggest lot there first, and raises — before moving
    anything — if it can't supply them. Copies already sleeved here still
    count first. A starter lot is never made for a chosen Location.

    `source="buy"` (Phase 4) moves and makes nothing: it returns one `buy`
    move for the copies still to be bought, the same on any deck.

    `source="deck"` (Phase 3) takes copies sleeved in the deck named by
    `source_location` — only those its list no longer runs, so call it after
    that deck's list has given the card up (transfer_between_decks() and a
    linked pair's apply do). Short of them it raises, like a storage pick.
    Auto never takes a copy out of another deck: only this explicit choice
    does.

    A brewing or dismantled deck (rule 4) changes its list only: no lot is
    touched and one placeholder move comes back instead, unless `force`
    (Phase 5's "Build it", the only caller that sets it).

    A basic land (Phase 7) is checked first, before the source is even
    read: `quantity` copies join the list from the "basic land supply" —
    one logged `bulk` move, no lot pulled, split or made, whatever the
    collection holds and whatever the deck's build state.

    `reason` / `change_id` go on the move log's rows for the lots touched
    (Phase 6): "deck_edit" for a hand edit, "apply" from apply_change(),
    with the deck_changes row that caused it."""
    deck_name, state = _deck_name_and_state(conn, deck_id)
    card = _card_name(conn, scryfall_id) or scryfall_id
    if q.is_basic_land(conn, scryfall_id=scryfall_id):
        count = min(int(quantity), q.deck_list_quantity(conn, deck_id, scryfall_id))
        return ([_bulk_move(conn, deck_name, scryfall_id, card, count, "in", reason, change_id)]
                if count > 0 else [])
    source, source_location = normalize_source(conn, source, source_location)
    here = sum(qty for _, qty, loc, _ in q.card_lots(conn, scryfall_id) if (loc or "").strip() == deck_name)
    need = min(int(quantity), q.deck_list_quantity(conn, deck_id, scryfall_id) - here)
    if need <= 0:
        return []
    if source == "buy":
        # A planned purchase (Phase 4): the list has it, no lot exists yet and
        # none is made — the copy is sleeved when it's bought (record_purchase),
        # whatever the deck's build state.
        return [_move(card, need, "buy", frm=None, to=None, direction="in")]
    if state in q.BUILD_STATES and not force:
        # The chosen Location rides along so "will be pulled from Lands Box"
        # can say so, and so Build it (Phase 5) has it on the move.
        return [_move(card, need, state, frm=state, to=state, direction="in",
                      source_location=source_location)]
    if source == "deck":
        free = _spare_in_deck(conn, source_location, scryfall_id)
        if sum(lot[1] for lot in free) < need:
            raise ValueError(
                f"{card}: {source_location} has {sum(lot[1] for lot in free)} sleeved cop"
                f"{'y' if sum(lot[1] for lot in free) == 1 else 'ies'} it no longer needs, {need} needed."
            )
    else:
        free = [(cid, qty, loc) for cid, qty, loc, _ in
                q.free_lots(conn, scryfall_id, location=source_location if source else None)]
    if source == "storage" and sum(lot[1] for lot in free) < need:
        raise ValueError(
            f"{card}: {source_location} has {sum(lot[1] for lot in free)} free, {need} needed. "
            "Pick another Location, or leave it on Auto."
        )
    moves = []
    for collection_id, have, location in free:
        if need <= 0:
            break
        take = min(have, need)
        moved_id = move_lot(conn, collection_id, deck_name, quantity=take,
                            reason=reason, change_id=change_id)
        moves.append(_move(card, take, "move", moved_id, location, deck_name))
        need -= take
    if need > 0:   # only reachable on Auto: a chosen Location or deck is checked above
        lot_id = add_collection_lot(
            conn, scryfall_id, quantity=need, foil=False, location=deck_name,
            date_acquired=datetime.date.today().isoformat(), price_paid=None,
            source=STARTER_LOT_SOURCE, reason=reason, change_id=change_id,
        )
        moves.append(_move(card, need, "new", lot_id, None, deck_name))
    return moves


def unsleeve_from_deck(conn, deck_id, scryfall_id, quantity=None, dest=None, force=False,
                       reason="deck_edit", change_id=None):
    """Move up to `quantity` copies of a card (all of them when None) out of
    a deck's sleeves to `dest` (default DEFAULT_LOCATION, "Box") and return
    the moves made. Call it AFTER the list change: only copies the list no
    longer runs come out, so −1 on a deck that was never fully sleeved
    moves nothing, and cutting one printing for another of the same card
    leaves the copy where it is.

    Lots of the cut printing come out first, then the rest in
    collection_id order; a lot holding more than is needed is split.

    A brewing deck changes its list only (one placeholder move), unless
    `force`. A dismantled deck is NOT held back here, unlike
    sleeve_into_deck(): its list is a record and nothing must be pulled
    back in, but a copy still physically in its sleeves is exactly what
    taking it apart moves out — and the Deck Editor marks a deck dismantled
    when the dismantle is staged, before its removals are applied.

    A basic land (Phase 7) leaves for the "basic land supply": `quantity`
    copies (every caller passes what the list just lost), one logged `bulk`
    move, no lot put away — a tracked lot of Swamps stays where it is.

    `reason` / `change_id` as for sleeve_into_deck()."""
    deck_name, state = _deck_name_and_state(conn, deck_id)
    dest = dest or DEFAULT_LOCATION
    card = _card_name(conn, scryfall_id) or scryfall_id
    if q.is_basic_land(conn, scryfall_id=scryfall_id):
        count = int(quantity or 0)
        return ([_bulk_move(conn, deck_name, scryfall_id, card, count, "out", reason, change_id)]
                if count > 0 else [])
    here = [lot for lot in q.card_lots(conn, scryfall_id) if (lot[2] or "").strip() == deck_name]
    surplus = sum(lot[1] for lot in here) - q.deck_list_quantity(conn, deck_id, scryfall_id)
    count = surplus if quantity is None else min(int(quantity), surplus)
    if count <= 0:
        return []
    if state == "brewing" and not force:
        return [_move(card, count, state, frm=state, to=state, direction="out")]

    here.sort(key=lambda lot: (lot[3] != scryfall_id, lot[0]))
    moves = []
    for collection_id, have, location, _sid in here:
        if count <= 0:
            break
        take = min(have, count)
        moved_id = move_lot(conn, collection_id, dest, quantity=take,
                            reason=reason, change_id=change_id)
        moves.append(_move(card, take, "move", moved_id, location, dest))
        count -= take
    return moves


def move_copies(conn, collection_id, location, quantity=None, reason="reconcile"):
    """move_lot() for the lot-only moves that change no list — Workbench
    Reconcile's "Sleeved here" / "Put away" and a dismantle's strays — so
    they report through the same move dicts as a deck edit. Returns
    [move]. `reason` is the move log's (Phase 6): "reconcile" by default,
    "dismantle" for a dismantle's strays."""
    row = conn.execute(
        """SELECT c.name, col.location, col.quantity FROM collection col
           LEFT JOIN cards c ON c.scryfall_id = col.scryfall_id WHERE col.collection_id=?""",
        (collection_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"No collection lot #{collection_id}.")
    card, old_location, have = row
    moved_id = move_lot(conn, collection_id, location, quantity=quantity, reason=reason)
    moved = have if quantity is None or have is None else min(int(quantity), have)
    return [_move(card or "(unknown card)", moved, "move", moved_id, old_location, location)]


def revert_moves(conn, moves):
    """Undo a list of moves, newest first: each moved lot goes back to its
    `from`, and a starter lot is deleted again — only if it is untouched
    since (still at `to`, same quantity). Placeholder moves are skipped;
    there's no lot to put back.

    A lot that move_lot() split stays as two lots after the undo — same
    printing, same location, harmless, so no merge logic.

    A basic land's `bulk` move (Phase 7) has no lot to put back: the caller
    already reversed the list, so this logs the opposite bulk step.

    Returns (moves, skipped): the moves made by the undo itself (for the
    pop-up), and one line per move that couldn't be undone because the lot
    has moved or changed since."""
    done, skipped = [], []
    for m in reversed(list(moves or [])):
        kind = m.get("kind")
        if kind == "bulk" and m.get("scryfall_id") and m.get("direction") in ("in", "out"):
            deck_name = m["to"] if m["direction"] == "in" else m["from"]
            done.append(_bulk_move(conn, deck_name, m["scryfall_id"], m["card"], m["quantity"],
                                   "out" if m["direction"] == "in" else "in"))
            continue
        if kind not in ("move", "new"):
            continue
        row = conn.execute(
            "SELECT location, quantity FROM collection WHERE collection_id=?", (m["collection_id"],)
        ).fetchone()
        if row is None or (row[0] or "") != (m["to"] or ""):
            skipped.append(f"{m['card']}: not in {m['to'] or 'its old place'} any more — left where it is.")
            continue
        if kind == "new":
            if row[1] != m["quantity"]:
                skipped.append(f"{m['card']}: its new copy record was edited since — kept.")
                continue
            remove_collection_lot(conn, m["collection_id"], reason="deck_edit")
            done.append(_move(m["card"], m["quantity"], "delete", m["collection_id"], m["to"], None))
            continue
        moved_id = move_lot(conn, m["collection_id"], m["from"], quantity=m["quantity"],
                            reason="deck_edit")
        done.append(_move(m["card"], m["quantity"], "move", moved_id, m["to"], m["from"]))
    return done, skipped


# ------------------------------------------------------------------
# Deck changes — the idea -> planned -> applied / dropped lifecycle
# (Workbench rework, Phase 1; table in schema.sql, plan in
# docs/archive/IMPLEMENTATION_PLAN.md). One table replaces what used to be the
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
    # Phase 2: where the add comes from / where the cut copy goes.
    "source", "source_location", "dest_location",
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
               review_flag=0, notes=None, status="idea",
               source=None, source_location=None, dest_location=None):
    """Insert an idea (default) or an already-paired planned swap. Returns
    the new change_id. Raises ValueError for a duplicate open row on the
    same card, a planned row with no card to cut, or a row that does
    nothing at all.

    `source` / `source_location` (Phase 2) save where the add's copy should
    come from for when the row is applied (None = Auto); `dest_location`
    where the cut copy goes (None = Box). See normalize_source /
    normalize_dest for what is refused.

    source="deck" (Phase 3) is a take from another deck, written as a
    linked pair by add_linked_take(); its change_id (the receiving row's)
    is returned the same way."""
    source, source_location = normalize_source(conn, source, source_location, deck_id=deck_id,
                                               scryfall_id=add_scryfall_id)
    dest_location = normalize_dest(conn, dest_location, scryfall_id=remove_scryfall_id)
    if status not in OPEN_CHANGE_STATUSES:
        raise ValueError("A new change starts as an idea or a planned swap.")
    if not (add_scryfall_id or add_name or remove_scryfall_id):
        raise ValueError("A change has to add a card, remove one, or both.")
    if source == "deck":
        if not add_scryfall_id:
            raise ValueError("Pick the card's printing before taking it from another deck.")
        return add_linked_take(conn, deck_id, _deck_id_by_name(conn, source_location), add_scryfall_id,
                               quantity, status, add_name=add_name, remove_scryfall_id=remove_scryfall_id,
                               remove_name=remove_name, notes=notes, review_flag=review_flag,
                               dest_location=dest_location)
    if status == "planned" and not remove_scryfall_id:
        raise ValueError("A planned swap needs a card to replace.")
    if int(quantity) < 1:
        raise ValueError("Quantity must be at least 1.")
    if add_scryfall_id and _open_change_for_card(conn, deck_id, add_scryfall_id):
        raise ValueError(f"{add_name or add_scryfall_id} is already on this deck's shortlist.")

    cur = conn.execute(
        """INSERT INTO deck_changes
               (deck_id, status, add_scryfall_id, add_name, remove_scryfall_id,
                remove_name, quantity, review_flag, notes, planned_at,
                source, source_location, dest_location)
           VALUES (?,?,?,?,?,?,?,?,?, CASE WHEN ? = 'planned' THEN CURRENT_TIMESTAMP END, ?,?,?)""",
        (deck_id, status, add_scryfall_id, add_name, remove_scryfall_id,
         remove_name, int(quantity), 1 if review_flag else 0, notes, status,
         source, source_location, dest_location),
    )
    conn.commit()
    return cur.lastrowid


def _printing_by_name(conn, name, owned_first=True):
    """(scryfall_id, name) of the database's printing of a card by name,
    matching the front face of an MDFC ("Front // Back"); a printing you own
    comes first. None when nothing matches."""
    name = (name or "").strip()
    if not name:
        return None
    rows = conn.execute(
        """SELECT c.scryfall_id, c.name,
                  EXISTS (SELECT 1 FROM collection col WHERE col.scryfall_id = c.scryfall_id) AS owned
           FROM cards c WHERE c.name = ? COLLATE NOCASE OR c.name LIKE ? COLLATE NOCASE
           ORDER BY owned DESC, c.scryfall_id LIMIT 1""", (name, f"{name} // %")).fetchall()
    return (rows[0][0], rows[0][1]) if rows else None


def add_advisor_idea(conn, deck_id, card, replaces=None, skill=None):
    """Stage an Advisor suggestion as a Shortlist idea (EDIT_FLOW_PLAN.md
    Phase 5): `card` is the add, `replaces` (when it names a card on the
    deck's list) fills in the idea's "Replaces", and the note says which
    skill suggested it. The Advisor never touches the deck's list or the
    collection — an idea only becomes a change when you plan and apply it.
    Returns the change_id. Raises ValueError, writing nothing, when the card
    isn't in the database, is already on the deck, or is already on its
    shortlist."""
    found = _printing_by_name(conn, card)
    if found is None:
        raise ValueError(f"{card} isn't in the card database.")
    add_sid, add_name = found
    if q.deck_list_quantity(conn, deck_id, add_sid) > 0:
        raise ValueError(f"{add_name} is already in this deck.")
    remove_sid = remove_name = None
    if (replaces or "").strip():
        on_list = _printing_by_name(conn, replaces)
        if on_list and q.deck_list_printing(conn, deck_id, on_list[0]) is not None:
            remove_sid = q.deck_list_printing(conn, deck_id, on_list[0])[0]
            remove_name = _card_name(conn, remove_sid)
    return add_change(conn, deck_id, add_scryfall_id=add_sid, add_name=add_name,
                      remove_scryfall_id=remove_sid, remove_name=remove_name,
                      notes=f"Advisor: {skill}" if skill else "Advisor")


def _log_applied_change(conn, deck_id, add_scryfall_id=None, remove_scryfall_id=None, quantity=1,
                        notes=None, source=None, source_location=None, dest_location=None):
    """Record an already-completed hand edit (a mainboard add or remove,
    or — Phase 0 — a quantity bump on a card already on the list) as an
    'applied' row, so history covers more than swaps. Not for the
    shortlist flow: apply_change() flips its own row to applied instead.
    `source` / `source_location` / `dest_location` (Phase 2) are the
    choices the edit made, already normalized; None when it left them on
    Auto / Box."""
    cur = conn.execute(
        """INSERT INTO deck_changes
               (deck_id, status, add_scryfall_id, add_name, remove_scryfall_id,
                remove_name, quantity, notes, resolved_at, source, source_location, dest_location)
           VALUES (?, 'applied', ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)""",
        (deck_id, add_scryfall_id, _card_name(conn, add_scryfall_id),
         remove_scryfall_id, _card_name(conn, remove_scryfall_id), quantity, notes,
         source, source_location, dest_location),
    )
    conn.commit()
    return cur.lastrowid  # the move log's change_id for the copies this edit moves


def update_change(conn, change_id, **fields):
    """Edit an open row's editable columns. Every check runs before anything
    is written.

    Phase 3: on a take's receiving row, a new `source` re-links the pair —
    away from a deck drops the promised removal on it, onto a deck (or
    another one) writes a fresh one, checked like a new take — and a new
    `quantity` is copied to the donor's half. The donor's half can't be
    pointed somewhere else (`dest_location`): the copy is promised."""
    fields = {k: v for k, v in fields.items() if k in _CHANGE_EDITABLE}
    if not fields:
        return
    row = _require_change(conn, change_id)
    if "review_flag" in fields:
        fields["review_flag"] = 1 if fields["review_flag"] else 0
    relink = None
    if "source" in fields or "source_location" in fields:
        # The pair is validated together, against what the row already
        # holds for whichever half the caller left out.
        fields["source"], fields["source_location"] = normalize_source(
            conn, fields.get("source", row["source"]), fields.get("source_location", row["source_location"]),
            deck_id=row["deck_id"])
        if ((fields["source"], fields["source_location"]) != (row["source"], row["source_location"])
                and row["status"] in OPEN_CHANGE_STATUSES):
            relink = {"ctx": None}
            if fields["source"] == "deck":
                if not (fields.get("add_scryfall_id") or row["add_scryfall_id"]):
                    raise ValueError("Pick the card's printing before taking it from another deck.")
                ctx = _take_context(conn, _deck_id_by_name(conn, fields["source_location"]), row["deck_id"],
                                    fields.get("add_scryfall_id") or row["add_scryfall_id"],
                                    fields.get("quantity") or row["quantity"] or 1)
                _refuse_second_take(conn, ctx)
                relink["ctx"] = ctx
    partner = _open_partner(conn, row)
    if "dest_location" in fields:
        if partner and q.is_donor_row(row) and fields["dest_location"] != row["dest_location"]:
            raise ValueError(_donor_refusal(conn, row))
        fields["dest_location"] = normalize_dest(conn, fields["dest_location"])
    if "quantity" in fields and partner and relink is None:
        donor = row if q.is_donor_row(row) else partner
        listed = _deck_card_quantity(conn, donor["deck_id"], donor["remove_scryfall_id"]) or 0
        if int(fields["quantity"]) > listed:
            raise ValueError(f"{_deck_name_and_state(conn, donor['deck_id'])[0]} only runs "
                             f"{donor['remove_name'] or 'that card'} ×{listed}.")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE deck_changes SET {set_clause} WHERE change_id=?",
        (*fields.values(), change_id),
    )
    if relink is not None:
        if partner:
            _set_dropped(conn, partner["change_id"], TAKE_DROPPED_NOTE)
        conn.execute("UPDATE deck_changes SET linked_change_id=NULL WHERE change_id=?", (change_id,))
        if relink["ctx"]:
            _link(conn, _insert_donor_removal(conn, relink["ctx"]), change_id)
    elif "quantity" in fields and partner:
        conn.execute("UPDATE deck_changes SET quantity=? WHERE change_id=?",
                     (int(fields["quantity"]), partner["change_id"]))
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
    # A take from another deck (Phase 3) can be planned as a plain add: the
    # card it brings comes with its own removal, on the deck it leaves.
    if not target and row["source"] != "deck":
        raise ValueError("Pick a card for this to replace before planning it.")
    target_name = (remove_name or _card_name(conn, target) or row["remove_name"]) if target else None
    conn.execute(
        """UPDATE deck_changes
           SET status='planned', planned_at=CURRENT_TIMESTAMP,
               remove_scryfall_id=?, remove_name=?, quantity=COALESCE(?, quantity)
           WHERE change_id=?""",
        (target, target_name, int(quantity) if quantity is not None else None, change_id),
    )
    conn.commit()


def demote_change(conn, change_id):
    """planned -> idea, keeping the pairing so re-planning it is one click.
    A take's receiving row keeps its promised removal on the donor (Phase 3:
    the donor's half is always planned, so the copy stays promised); the
    donor's half itself can't go back to an idea — it isn't this deck's
    plan — so that is refused."""
    row = _require_change(conn, change_id, "planned")
    if row["linked_change_id"] and q.is_donor_row(row):
        raise ValueError(_donor_refusal(conn, row))
    conn.execute(
        "UPDATE deck_changes SET status='idea', planned_at=NULL WHERE change_id=?", (change_id,)
    )
    conn.commit()


def drop_change(conn, change_id):
    """idea/planned -> dropped. Never deletes: the row is the memory of why
    a card lost, so it isn't re-evaluated from scratch next month.

    Half of a take from another deck (Phase 3) takes its open partner with
    it — a promised removal with no add to feed, or an add whose copy is no
    longer promised. An already-applied partner is left alone (a brewing
    deck's list kept the card; it just needs another source now). Returns a
    line per partner dropped, naming it, for the caller's message ([] when
    there was none)."""
    row = _require_change(conn, change_id, *OPEN_CHANGE_STATUSES)
    _set_dropped(conn, change_id)
    lines = []
    partner = _open_partner(conn, row)
    if partner:
        _set_dropped(conn, partner["change_id"], TAKE_DROPPED_NOTE)
        lines.append(_partner_line(conn, partner, "Also dropped"))
    conn.commit()
    return lines


def _set_dropped(conn, change_id, note=None):
    """Mark one row dropped, appending `note` to its notes when given. No
    commit — the caller's transition commits."""
    conn.execute(
        """UPDATE deck_changes
              SET status='dropped', resolved_at=CURRENT_TIMESTAMP,
                  notes = CASE
                      WHEN ? IS NULL THEN notes
                      WHEN notes IS NULL OR TRIM(notes) = '' THEN ?
                      ELSE notes || ' — ' || ?
                  END
            WHERE change_id = ?""",
        (note, note, note, change_id),
    )


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
    add_change() exists to prevent.

    A take from another deck (Phase 3) comes back with a fresh promised
    removal on the donor, checked like a new take; if the donor can't give
    the card any more it comes back on Auto instead, and the returned line
    says so. The donor's half of a take can't be reopened on its own — it
    is the receiving deck's to reopen. Returns lines for the message."""
    row = _require_change(conn, change_id, "dropped")
    if row["linked_change_id"] and q.is_donor_row(row):
        raise ValueError(_donor_refusal(conn, row))
    if row["add_scryfall_id"] and _open_change_for_card(conn, row["deck_id"], row["add_scryfall_id"]):
        raise ValueError(f"{row['add_name'] or 'That card'} is already back on the shortlist.")
    lines = []
    donor_id = None
    if row["source"] == "deck":
        try:
            ctx = _take_context(conn, _deck_id_by_name(conn, row["source_location"]), row["deck_id"],
                                row["add_scryfall_id"], row["quantity"] or 1)
            _refuse_second_take(conn, ctx)
        except ValueError as exc:
            conn.execute("UPDATE deck_changes SET source=NULL, source_location=NULL, linked_change_id=NULL "
                         "WHERE change_id=?", (change_id,))
            lines.append(f"Brought back on Auto: {exc}")
        else:
            donor_id = _insert_donor_removal(conn, ctx)
    conn.execute(
        "UPDATE deck_changes SET status='idea', planned_at=NULL, resolved_at=NULL WHERE change_id=?",
        (change_id,),
    )
    if donor_id:
        _link(conn, donor_id, change_id)
        lines.append(f"{row['source_location']}'s copy is promised again.")
    conn.commit()
    return lines


def _move_outcome(moves_out, moves_in):
    """execute_swap()'s long-standing {"moved_out", "assigned_lot_id",
    "created_lot"} summary, kept beside the full "moves" list (cut side
    first) so callers written before Phase 1 still read it."""
    real_in = [m for m in moves_in if m["kind"] in ("move", "new")]
    return {
        "moved_out": any(m["kind"] == "move" for m in moves_out),
        "assigned_lot_id": real_in[0]["collection_id"] if real_in else None,
        "created_lot": any(m["kind"] == "new" for m in moves_in),
        "moves": list(moves_out) + list(moves_in),
    }


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
    "assigned_lot_id", "created_lot", "moves"} — so one caller can report
    all three. Copies move through sleeve_into_deck() / unsleeve_from_deck()
    (EDIT_FLOW_PLAN.md Phase 1), the same as a hand edit.

    NOT atomic across the whole change: the writes commit as they go, so an
    unexpected failure part-way (a database error, not one of the checks
    above) can leave the deck changed while the row is still 'planned'. The
    checks above run first precisely so that the expected failures happen
    before anything is touched.

    Phase 3: half of a take from another deck applies its open partner too,
    removal first, with both sides checked before either is written — see
    _apply_pair(). Its outcome adds "pair": {"card", "from_deck", "to_deck",
    "held", "partner_id"}."""
    row = _require_change(conn, change_id, "planned")
    partner = _open_partner(conn, row)
    if partner is not None:
        return _apply_pair(conn, row, partner, add_scryfall_id)
    return _execute_apply(conn, _prepare_apply(conn, row, add_scryfall_id))


def _removal_count(conn, row, deck_state):
    """Copies a planned removal takes off its deck's list. A take's donor
    half (Phase 3) gives up only the copies taken — Gisa running Rats ×4
    and handing two over keeps two. Any other removal, a dismantle's
    included, takes the whole entry: a dismantle's row may carry fewer
    copies than the list did (see stage_dismantle())."""
    listed = _deck_card_quantity(conn, row["deck_id"], row["remove_scryfall_id"]) or 0
    quantity = row["quantity"] or 1
    if row.get("linked_change_id") and deck_state != "dismantled":
        return min(quantity, listed)
    return max(quantity, listed)


def _prepare_apply(conn, row, add_scryfall_id=None, donor_removing=0):
    """apply_change()'s checks for one row, raising before anything is
    written; returns what _execute_apply() needs. `donor_removing` is how
    many copies the donor's half of a pair is about to give up, for a
    receiving row's "can the donor hand it over" check."""
    add_sid = row["add_scryfall_id"] or add_scryfall_id
    remove_sid = row["remove_scryfall_id"]
    # An add side that exists but hasn't resolved to a printing is an error,
    # not an absent add side — see the docstring.
    if not add_sid and (row["add_name"] or "").strip():
        raise ValueError(f"{row['add_name']} isn't linked to a printing yet.")
    if not add_sid and not remove_sid:
        raise ValueError("The card to add isn't linked to a printing yet.")

    deck_id = row["deck_id"]
    deck_name, deck_state = _deck_name_and_state(conn, deck_id)
    quantity = row["quantity"] or 1
    ctx = {
        "row": row, "deck_id": deck_id, "deck_name": deck_name, "add_sid": add_sid,
        "remove_sid": remove_sid, "quantity": quantity,
        "add_card_name": _card_name(conn, add_sid) or row["add_name"],
        "remove_card_name": _card_name(conn, remove_sid) or row["remove_name"],
        # Phase 6 rows name their destination; everything older means storage.
        "dest": (row["dest_location"] if "dest_location" in row else None) or DEFAULT_LOCATION,
        "cut_count": 0,
    }

    if remove_sid and _deck_card_quantity(conn, deck_id, remove_sid) is None:
        raise ValueError(
            f"{ctx['remove_card_name']} is no longer in this deck's mainboard, so there is nothing to "
            "replace. Send it back to an idea or drop it."
        )
    if remove_sid and not add_sid:
        ctx["cut_count"] = _removal_count(conn, row, deck_state)

    # A chosen storage Location (Phase 2) or donor deck (Phase 3) is checked
    # before anything is written, so one that can't supply the copy any more
    # refuses the apply cleanly instead of leaving the list changed and
    # nothing sleeved.
    source, source_location = (row["source"], row["source_location"]) if add_sid else (None, None)
    ctx["source"], ctx["source_location"] = source, source_location
    if source:
        list_after = (q.deck_list_quantity(conn, deck_id, add_sid)
                      - (_deck_card_quantity(conn, deck_id, add_sid) or 0) + quantity)
        if remove_sid and remove_sid != add_sid and (
                q.oracle_ids_for_card(conn, scryfall_id=remove_sid)
                == q.oracle_ids_for_card(conn, scryfall_id=add_sid)):
            list_after -= _deck_card_quantity(conn, deck_id, remove_sid) or 0
        if source == "deck":
            _check_deck_stock(conn, deck_id, add_sid, list_after, source_location, donor_removing)
        elif source == "storage":
            _check_stock(conn, deck_id, add_sid, list_after, source_location)
    return ctx


def _execute_apply(conn, ctx):
    """apply_change()'s writes for one row _prepare_apply() checked."""
    row, deck_id, deck_name = ctx["row"], ctx["deck_id"], ctx["deck_name"]
    add_sid, remove_sid, quantity = ctx["add_sid"], ctx["remove_sid"], ctx["quantity"]
    change_id = row["change_id"]
    source, source_location = ctx["source"], ctx["source_location"]
    if add_sid and remove_sid:
        outcome = execute_swap(
            conn, deck_id, deck_name,
            remove_scryfall_id=remove_sid, remove_card_name=ctx["remove_card_name"],
            add_scryfall_id=add_sid, add_card_name=ctx["add_card_name"], quantity=quantity,
            default_location=ctx["dest"], reason="apply", change_id=change_id,
            source=source, source_location=source_location,
        )
    elif remove_sid:
        cut = ctx["cut_count"]
        _reduce_deck_card(conn, deck_id, remove_sid, cut)
        outcome = _move_outcome(unsleeve_from_deck(conn, deck_id, remove_sid, cut, dest=ctx["dest"],
                                                   reason="apply", change_id=change_id), [])
        _drop_promises(conn, deck_id, remove_sid)
    else:
        _put_deck_card(conn, deck_id, add_sid, quantity)
        outcome = _move_outcome([], sleeve_into_deck(conn, deck_id, add_sid, quantity,
                                                     source=source, source_location=source_location,
                                                     reason="apply", change_id=change_id))

    conn.execute(
        """UPDATE deck_changes
           SET status='applied', resolved_at=CURRENT_TIMESTAMP,
               add_scryfall_id=?, add_name=?, remove_name=?
           WHERE change_id=?""",
        (add_sid, ctx["add_card_name"], ctx["remove_card_name"], change_id),
    )
    conn.commit()
    return outcome


def _apply_pair(conn, row, partner, add_scryfall_id=None):
    """Apply both open halves of a take (Phase 3), whichever was asked for.

    The receiving half has to be planned — an idea there isn't a decision
    yet, so the pair is refused with a reason naming that deck. Then:

      - the receiver is BREWING: only its add applies (the brew's list gains
        the card), and the donor's removal stays planned, so Gisa keeps its
        copy until the brew is built — the same lopsided pair a direct take
        on a brewing deck writes.
      - otherwise both apply, the donor's removal first: its copy moves
        donor → receiver with no stop in the box (dest_location is the
        receiving deck), and the receiver's sleeve step finds it already
        there. Both rows are checked before either is written.

    Returns the receiver's outcome with the donor's moves in front and
    "pair" describing it."""
    donor, receiver = (row, partner) if q.is_donor_row(row) else (partner, row)
    receiver_add = add_scryfall_id if receiver is row else None
    from_deck = _deck_name_and_state(conn, donor["deck_id"])[0]
    to_deck, to_state = _deck_name_and_state(conn, receiver["deck_id"])
    card = receiver["add_name"] or _card_name(conn, receiver["add_scryfall_id"]) or donor["remove_name"]
    if receiver["status"] != "planned":
        raise ValueError(f"{card}: {to_deck}'s side of this take is still an idea — plan it on "
                         f"{to_deck} first, or drop it.")
    pair = {"card": card, "from_deck": from_deck, "to_deck": to_deck, "held": False,
            "partner_id": partner["change_id"]}
    if to_state == "brewing":
        outcome = _execute_apply(conn, _prepare_apply(conn, receiver, receiver_add))
        pair["held"] = True
        return dict(outcome, pair=pair)
    d_ctx = _prepare_apply(conn, donor)
    r_ctx = _prepare_apply(conn, receiver, receiver_add, donor_removing=d_ctx["cut_count"])
    out_d = _execute_apply(conn, d_ctx)
    out_r = _execute_apply(conn, r_ctx)
    return dict(out_r, moved_out=out_d["moved_out"] or out_r["moved_out"],
                moves=out_d["moves"] + out_r["moves"], pair=pair)


def apply_changes(conn, change_ids, resolve_add=None):
    """Apply several planned swaps in planned order. One failing doesn't stop
    the rest. `resolve_add(name) -> (scryfall_id, was_new, error)` links a row
    that has no printing yet (card_resolver.resolve_or_fetch_card fits);
    without it such a row just fails with a clear message.

    Returns one dict per change: {"change_id", "ok", "add_name", "remove_name",
    "was_new", "outcome", "moves", "error", "pair"}. `outcome` is execute_swap's
    result; `moves` is its move list ([] on failure), for finish_edit().

    Phase 3: a take's two halves apply together, so a batch naming both gets
    ONE result for the pair (`pair` from apply_change(), None otherwise) —
    the second half is skipped, not reported as "already applied"."""
    rows = [get_change(conn, cid) for cid in change_ids]
    rows = sorted((r for r in rows if r), key=lambda r: (r["planned_at"] or r["created_at"], r["change_id"]))
    results, done = [], set()
    for row in rows:
        if row["change_id"] in done:
            continue
        result = {
            "change_id": row["change_id"], "ok": False,
            "add_name": row["add_name"] or _card_name(conn, row["add_scryfall_id"]),
            "remove_name": row["remove_name"] or _card_name(conn, row["remove_scryfall_id"]),
            "was_new": False, "outcome": None, "moves": [], "error": None, "pair": None,
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
            result["moves"] = result["outcome"]["moves"]
            result["pair"] = result["outcome"].get("pair")
            if result["pair"]:
                # Applied with it — or, for a brew, deliberately left
                # planned, which applying it here as well would undo.
                done.add(result["pair"]["partner_id"])
            result["ok"] = True
        except ValueError as exc:
            result["error"] = str(exc)
        results.append(result)
    return results


# ------------------------------------------------------------------
# Taking a card from another deck (EDIT_FLOW_PLAN.md Phase 3).
#
# A take is a linked PAIR of deck_changes rows, each naming the other in
# linked_change_id:
#
#   donor half     a removal on the deck giving the card up (no add side),
#                  dest_location = the receiving deck, so applying it moves
#                  the lot donor → receiver with no stop in the box
#   receiving half an add on the deck getting it, source='deck',
#                  source_location = the donor
#
# The same shape a dismantle transfer has always written; Phase 3 makes the
# pairing explicit instead of a matter of change_id order and a note.
#
# Pair rules, enforced here and in the lifecycle transitions above:
#   - a direct take (transfer_between_decks) writes both rows applied at
#     once — unless the receiver is brewing, when its add applies (the list
#     changes) and the donor's removal waits, planned, for Build it
#   - a staged take (add_linked_take) writes the receiving row as an idea or
#     planned add and the donor's half as PLANNED, so the copy is promised
#     (the allocator and the donor's Board both show it) from the start
#   - applying either half applies both, removal first; dropping either
#     drops both; an already-applied half is never touched again
#   - a deck has at most one open take of one of its cards
# ------------------------------------------------------------------
TAKE_DROPPED_NOTE = "take cancelled"


def _deck_id_by_name(conn, name):
    row = conn.execute("SELECT deck_id FROM decks WHERE name=?", ((name or "").strip(),)).fetchone()
    if row is None:
        raise ValueError(f"There's no deck called {name!r}.")
    return row[0]


def _sleeved_in(conn, deck_name, scryfall_id):
    return sum(qty for _c, qty, loc, _s in q.card_lots(conn, scryfall_id) if (loc or "").strip() == deck_name)


def _spare_in_deck(conn, deck_name, scryfall_id):
    """The lots of a card sleeved in `deck_name` that its list no longer
    runs, trimmed to that surplus: [(collection_id, quantity, location)] —
    what sleeve_into_deck(source="deck") may take."""
    deck_id = _deck_id_by_name(conn, deck_name)
    lots = [(cid, qty, loc) for cid, qty, loc, _s in q.card_lots(conn, scryfall_id)
            if (loc or "").strip() == deck_name]
    spare = sum(lot[1] for lot in lots) - q.deck_list_quantity(conn, deck_id, scryfall_id)
    out = []
    for cid, qty, loc in lots:
        if spare <= 0:
            break
        take = min(qty, spare)
        out.append((cid, take, loc))
        spare -= take
    return out


def _check_deck_stock(conn, deck_id, scryfall_id, list_after, donor_name, donor_removing):
    """The take-from-a-deck twin of _check_stock(): refuse, before anything
    is written, when the donor can't hand over the copies this deck's list
    will need (`list_after` minus copies already sleeved here). The donor
    gives only what its list stops running once `donor_removing` copies come
    off it, so a deck whose copy was never recorded as sleeved there can't
    give one — say so, and how to fix it. Nothing to check for a brewing /
    dismantled receiver (list only) or a basic land (never moved)."""
    deck_name, state = _deck_name_and_state(conn, deck_id)
    if state in q.BUILD_STATES or q.is_basic_land(conn, scryfall_id=scryfall_id):
        return
    need = list_after - _sleeved_in(conn, deck_name, scryfall_id)
    if need <= 0:
        return
    donor_id = _deck_id_by_name(conn, donor_name)
    sleeved = _sleeved_in(conn, donor_name, scryfall_id)
    listed = q.deck_list_quantity(conn, donor_id, scryfall_id)
    spare = sleeved - (listed - donor_removing)
    if spare < need:
        card = _card_name(conn, scryfall_id) or scryfall_id
        raise ValueError(
            f"{card}: {donor_name} has no copy to hand over — its list runs ×{listed} and only "
            f"{sleeved} {'is' if sleeved == 1 else 'are'} recorded as sleeved there. Mark the copy as "
            f"sleeved in {donor_name} (Workbench → Reconcile) first, or take it from storage."
        )


def _reduce_deck_card(conn, deck_id, scryfall_id, count):
    """Take `count` copies of one printing off a deck's list, deleting the
    entry when none are left. No history row — the caller logs its own."""
    left = (_deck_card_quantity(conn, deck_id, scryfall_id) or 0) - int(count)
    if left > 0:
        _put_deck_card(conn, deck_id, scryfall_id, left)
    else:
        _delete_deck_card(conn, deck_id, scryfall_id)


def _open_partner(conn, row):
    """The other half of a take, when it is still idea/planned, else None."""
    linked = row.get("linked_change_id") if row else None
    partner = get_change(conn, linked) if linked else None
    return partner if partner and partner["status"] in OPEN_CHANGE_STATUSES else None


def _partner_line(conn, partner, verb):
    """"Also dropped Gisa's side: Sol Ring (it was going to Prosper)." for a
    pair transition's message."""
    deck = _deck_name_and_state(conn, partner["deck_id"])[0]
    if q.is_donor_row(partner):
        card = partner["remove_name"] or _card_name(conn, partner["remove_scryfall_id"])
        return f"{verb} {deck}'s side of the take: {card} stays in {deck}."
    card = partner["add_name"] or _card_name(conn, partner["add_scryfall_id"])
    return f"{verb} {deck}'s side of the take: {deck} no longer gets {card}."


def _donor_refusal(conn, row):
    """Why a donor's half can't be changed on its own: it's the receiving
    deck's plan."""
    partner = get_change(conn, row["linked_change_id"]) if row["linked_change_id"] else None
    to_deck = _deck_name_and_state(conn, partner["deck_id"])[0] if partner else "another deck"
    card = row["remove_name"] or _card_name(conn, row["remove_scryfall_id"]) or "This card"
    return (f"{card} is promised to {to_deck} — change it on {to_deck}'s side, or drop it to keep "
            "the card here.")


def _take_context(conn, from_deck_id, to_deck_id, scryfall_id, quantity):
    """Every check a take shares, raising before anything is written:
    different decks, a receiver that isn't dismantled (its list is a record),
    a donor whose list runs the card (any printing) at least `quantity`
    times. Returns {from_deck_id, from_name, from_state, to_deck_id,
    to_name, to_state, scryfall_id, donor_sid, quantity, card}; `donor_sid`
    is the printing the donor's list runs, which is what its removal names."""
    from_name, from_state = _deck_name_and_state(conn, from_deck_id)
    to_name, to_state = _deck_name_and_state(conn, to_deck_id)
    if from_deck_id == to_deck_id:
        raise ValueError("A deck can't take a card from itself.")
    if to_state == "dismantled":
        raise ValueError(f"{to_name} is dismantled — its list is a record, so it can't take cards from "
                         "another deck. Mark it no longer dismantled first.")
    quantity = int(quantity or 1)
    if quantity < 1:
        raise ValueError("Quantity must be at least 1.")
    card = _card_name(conn, scryfall_id) or scryfall_id
    on_list = q.deck_list_printing(conn, from_deck_id, scryfall_id)
    if on_list is None:
        raise ValueError(f"{from_name} doesn't run {card}, so there's nothing to take from it.")
    if quantity > on_list[1]:
        raise ValueError(f"{from_name} only runs {card} ×{on_list[1]}.")
    return {"from_deck_id": from_deck_id, "from_name": from_name, "from_state": from_state,
            "to_deck_id": to_deck_id, "to_name": to_name, "to_state": to_state,
            "scryfall_id": scryfall_id, "donor_sid": on_list[0], "quantity": quantity, "card": card}


def _refuse_second_take(conn, ctx, superseding=False):
    """A deck has at most one open removal of a card, so two decks can't
    both queue a take of Gisa's one copy (the allocator already shows the
    second as claimed_by before it gets here). `superseding` is a direct
    take replacing the receiver's own staged take of the card: that pair's
    donor half doesn't block it, and its receiving row's change_id is
    returned for the caller to retire. Returns [] otherwise."""
    superseded = []
    for change_id, linked in conn.execute(
        """SELECT change_id, linked_change_id FROM deck_changes
           WHERE deck_id=? AND remove_scryfall_id=? AND status IN ('idea','planned')""",
        (ctx["from_deck_id"], ctx["donor_sid"]),
    ).fetchall():
        partner = get_change(conn, linked) if linked else None
        if (superseding and partner and partner["deck_id"] == ctx["to_deck_id"]
                and partner["status"] in OPEN_CHANGE_STATUSES):
            superseded.append(partner["change_id"])
            continue
        whom = (f" (promised to {_deck_name_and_state(conn, partner['deck_id'])[0]})"
                if partner and partner["deck_id"] != ctx["from_deck_id"] else "")
        raise ValueError(f"{ctx['card']} is already queued to leave {ctx['from_name']}{whom}. "
                         "Drop that first, or take another card.")
    return superseded


def _insert_donor_removal(conn, ctx):
    """The donor's half of a take: a PLANNED removal of the donor's own
    printing, headed for the receiving deck. Returns its change_id; the
    caller links it. No commit."""
    cur = conn.execute(
        """INSERT INTO deck_changes
               (deck_id, status, remove_scryfall_id, remove_name, quantity, notes,
                planned_at, dest_location)
           VALUES (?, 'planned', ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)""",
        (ctx["from_deck_id"], ctx["donor_sid"], _card_name(conn, ctx["donor_sid"]), ctx["quantity"],
         f"Taken by {ctx['to_name']}", ctx["to_name"]),
    )
    return cur.lastrowid


def _link(conn, a, b):
    conn.execute("UPDATE deck_changes SET linked_change_id=? WHERE change_id=?", (b, a))
    conn.execute("UPDATE deck_changes SET linked_change_id=? WHERE change_id=?", (a, b))


def _drop_promises(conn, deck_id, scryfall_id):
    """A card has just come off a deck's list (any printing): drop every open
    removal on another deck that was still promising this deck a copy of it
    — the leftover half of a brewing deck's take (EDIT_FLOW_PLAN.md: "if you
    drop the card from the brew first, the planned removal on Gisa is dropped
    with it"). Only once the list runs none of the card: a brew going from ×2
    to ×1 still wants what was promised. Returns the change_ids dropped."""
    if q.deck_list_quantity(conn, deck_id, scryfall_id) > 0:
        return []
    card_oracles = q.oracle_ids_for_card(conn, scryfall_id=scryfall_id)
    dropped = []
    for take in q.open_takes(conn):
        if take["receiver_deck_id"] != deck_id or take["receiver_status"] != "applied":
            continue
        if card_oracles:
            same_card = q.oracle_ids_for_card(conn, scryfall_id=take["receiver_scryfall_id"]) == card_oracles
        else:
            same_card = take["receiver_scryfall_id"] == scryfall_id
        if same_card:
            _set_dropped(conn, take["donor_change_id"], TAKE_DROPPED_NOTE)
            dropped.append(take["donor_change_id"])
    if dropped:
        conn.commit()
        logger.info("Dropped %d promised removal(s) for a card leaving deck_id=%s: %s",
                    len(dropped), deck_id, dropped)
    return dropped


def transfer_between_decks(conn, from_deck_id, to_deck_id, scryfall_id, quantity=1, note=None):
    """Take `quantity` copies of a card out of another deck, right now — the
    direct edit behind "Comes from: Gisa" on the add bar and the Board. The
    card leaves Gisa's list and joins this one (adding to its quantity if
    it's already there), and Gisa's sleeved copy moves Gisa → here with no
    stop in the box: unsleeve_from_deck(dest=<this deck>), then
    sleeve_into_deck(source="deck"), which finds it already sleeved here.

    `scryfall_id` is the printing to add here; Gisa's removal names the
    printing Gisa's own list runs. Both decks get a linked applied history
    row — the add with source='deck', source_location=Gisa, the removal with
    dest_location=<this deck> — so History reads "from Gisa" / "to Prosper".

    A BREWING receiver changes its list only (rule 4): its add is applied,
    but Gisa's half is written as a linked PLANNED removal, so Gisa's list
    and sleeves stay as they are until the brew is built, and the copy shows
    as promised meanwhile. No stock check then — nothing moves.

    Raises ValueError, writing nothing, when the decks are the same, the
    receiver is dismantled, Gisa doesn't run the card that many times, Gisa
    already has an open removal of it (another take, or its own planned cut),
    or — for a non-brewing receiver — Gisa has no sleeved copy to hand over.
    A staged take of this card on THIS deck doesn't block it: this does what
    it planned, and it is retired (dropped, with its donor half) like any
    idea a hand edit satisfies.

    Returns {"moves", "card", "from_deck", "to_deck", "held",
    "receiver_change_id", "donor_change_id"}; `held` is True for a brewing
    receiver (Gisa's side waits)."""
    ctx = _take_context(conn, from_deck_id, to_deck_id, scryfall_id, quantity)
    superseded = _refuse_second_take(conn, ctx, superseding=True)
    quantity = ctx["quantity"]
    held = ctx["to_state"] == "brewing"
    existing = _deck_card_quantity(conn, to_deck_id, scryfall_id)
    if not held:
        _check_deck_stock(conn, to_deck_id, scryfall_id,
                          q.deck_list_quantity(conn, to_deck_id, scryfall_id) + quantity,
                          ctx["from_name"], quantity)
    new_qty = (existing or 0) + quantity
    notes = "; ".join(n for n in (f"quantity {existing} → {new_qty}" if existing else None, note) if n) or None

    for change_id in superseded:
        # This deck's own staged take of the card: done now, by hand.
        _set_dropped(conn, change_id, AUTO_DROP_NOTE)
        partner = _open_partner(conn, get_change(conn, change_id))
        if partner:
            _set_dropped(conn, partner["change_id"], TAKE_DROPPED_NOTE)
    _put_deck_card(conn, to_deck_id, scryfall_id, new_qty)
    if not held:
        _reduce_deck_card(conn, from_deck_id, ctx["donor_sid"], quantity)
        donor_id = _log_applied_change(conn, from_deck_id, remove_scryfall_id=ctx["donor_sid"],
                                       quantity=quantity, notes=f"taken by {ctx['to_name']}",
                                       dest_location=ctx["to_name"])
    receiver_id = _log_applied_change(conn, to_deck_id, add_scryfall_id=scryfall_id, quantity=quantity,
                                      notes=notes, source="deck", source_location=ctx["from_name"])
    if existing is None:
        # Retires this deck's own ideas for the card — a staged take of it
        # included, donor half and all (see the docstring).
        _auto_drop_stale_ideas(conn, to_deck_id, scryfall_id)
    if held:
        donor_id = _insert_donor_removal(conn, ctx)
    _link(conn, donor_id, receiver_id)
    conn.commit()

    moves = []
    if not held:
        moves += [m for m in unsleeve_from_deck(conn, from_deck_id, ctx["donor_sid"], quantity,
                                                dest=ctx["to_name"], change_id=donor_id)
                  # A brewing donor holds its copies back on the way out, but
                  # the receiver's sleeve step below still takes the one it
                  # no longer lists — so that placeholder would mislead.
                  if m["kind"] != "brewing"]
    moves += sleeve_into_deck(conn, to_deck_id, scryfall_id, quantity, source="deck",
                              source_location=ctx["from_name"], change_id=receiver_id)
    logger.info("Took %s x%d from deck_id=%d into deck_id=%d%s", ctx["card"], quantity,
                from_deck_id, to_deck_id, " (held: receiver is brewing)" if held else "")
    return {"moves": moves, "card": ctx["card"], "from_deck": ctx["from_name"], "to_deck": ctx["to_name"],
            "held": held, "receiver_change_id": receiver_id, "donor_change_id": donor_id}


def add_linked_take(conn, to_deck_id, from_deck_id, scryfall_id, quantity=1, status="idea", *,
                    add_name=None, remove_scryfall_id=None, remove_name=None, notes=None,
                    review_flag=0, dest_location=None):
    """Stage a take from another deck: the receiving row (an idea, or a
    planned add — planned needs no card to cut here, the card brings its own
    removal) plus the donor's half, a linked PLANNED removal on the donor with
    dest_location = the receiving deck. Written the way stage_dismantle()
    writes its pairs, removal first. Returns the receiving row's change_id.

    `remove_scryfall_id` / `remove_name` let the receiving row also name a
    card it replaces (a swap whose incoming card comes from Gisa), with
    `dest_location` for that cut copy.

    Raises ValueError, writing nothing: everything _take_context() checks,
    the card already on this deck's shortlist, or the donor already having
    an open removal of it — so two decks can't both queue a take of Gisa's
    one copy."""
    if status not in OPEN_CHANGE_STATUSES:
        raise ValueError("A new change starts as an idea or a planned swap.")
    ctx = _take_context(conn, from_deck_id, to_deck_id, scryfall_id, quantity)
    if _open_change_for_card(conn, to_deck_id, scryfall_id):
        raise ValueError(f"{add_name or ctx['card']} is already on {ctx['to_name']}'s shortlist.")
    _refuse_second_take(conn, ctx)
    donor_id = _insert_donor_removal(conn, ctx)
    cur = conn.execute(
        """INSERT INTO deck_changes
               (deck_id, status, add_scryfall_id, add_name, remove_scryfall_id,
                remove_name, quantity, review_flag, notes, planned_at,
                source, source_location, dest_location)
           VALUES (?,?,?,?,?,?,?,?,?, CASE WHEN ? = 'planned' THEN CURRENT_TIMESTAMP END, 'deck', ?, ?)""",
        (to_deck_id, status, scryfall_id, add_name or ctx["card"], remove_scryfall_id,
         remove_name or _card_name(conn, remove_scryfall_id), ctx["quantity"], 1 if review_flag else 0,
         notes, status, ctx["from_name"], dest_location),
    )
    _link(conn, donor_id, cur.lastrowid)
    conn.commit()
    logger.info("Staged take of %s x%d: deck_id=%d -> deck_id=%d (#%d / #%d)", ctx["card"],
                ctx["quantity"], from_deck_id, to_deck_id, donor_id, cur.lastrowid)
    return cur.lastrowid

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


# ------------------------------------------------------------------
# The move log (EDIT_FLOW_PLAN.md Phase 6, rule 2): every function below
# that changes a lot writes the move rows for what it did, in the same
# transaction as the change, so no page can move a copy without a record.
# Callers say WHY with `reason` (and the deck_changes row behind it with
# `change_id`); the default is a hand edit of the collection itself.
#
# from/to follow schema.sql: None = no copy on that side ("new copy
# record" / "deleted"), '' = the copy exists with no Location recorded.
# rename_deck() logs nothing — a rename relabels, no copy moved.
# ------------------------------------------------------------------
MOVE_REASONS = ("deck_edit", "apply", "reconcile", "dismantle", "collection_edit",
                "import", "prune", "deck_delete", "purchase", "build")


def _loc(value):
    """A Location as the log stores it: stripped, '' for none recorded."""
    return (value or "").strip()


def _log_collection_move(conn, collection_id, scryfall_id, quantity, frm, to, reason, change_id=None,
                         bulk=False):
    """One row in collection_moves. No commit: the caller's own commit
    carries it. No-op when nothing moved (same place both sides). `bulk`
    (Phase 7) marks a basic land's step, which has no lot: its None side is
    the "basic land supply", not "new copy record" / "deleted"."""
    if frm == to:
        return
    if reason not in MOVE_REASONS:
        raise ValueError(f"Unknown move reason {reason!r}.")
    conn.execute(
        """INSERT INTO collection_moves
               (collection_id, scryfall_id, card_name, quantity, from_location, to_location,
                reason, change_id, bulk)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (collection_id, scryfall_id, _card_name(conn, scryfall_id), quantity, frm, to,
         reason, change_id, 1 if bulk else 0),
    )


def add_collection_lot(conn, scryfall_id, *, reason="collection_edit", change_id=None, **fields):
    fields = {k: fields.get(k) for k in COLLECTION_FIELDS}
    fields["foil"] = 1 if fields.get("foil") else 0
    cur = conn.execute(
        """INSERT INTO collection (scryfall_id, quantity, foil, location, date_acquired, price_paid, source)
           VALUES (?,?,?,?,?,?,?)""",
        (scryfall_id, fields["quantity"], fields["foil"], fields["location"],
         fields["date_acquired"], fields["price_paid"], fields["source"]),
    )
    _log_collection_move(conn, cur.lastrowid, scryfall_id, fields["quantity"],
                         None, _loc(fields["location"]), reason, change_id)
    conn.commit()
    return cur.lastrowid


def update_collection_lot(conn, collection_id, *, reason="collection_edit", change_id=None, **fields):
    """Edit a lot's fields. Logs what a change to `location` or `quantity`
    means for the copies: a new Location moves the copies the lot held
    (the smaller of old and new quantity, when both change); more copies
    arrive as a "new copy record" at the new Location, fewer leave as
    "deleted" from where the lot was. A lot with no tracked quantity (NULL)
    logs only its Location move."""
    fields = {k: v for k, v in fields.items() if k in COLLECTION_FIELDS}
    if not fields:
        return
    old = conn.execute(
        "SELECT scryfall_id, quantity, location FROM collection WHERE collection_id=?",
        (collection_id,),
    ).fetchone()
    if "foil" in fields:
        fields["foil"] = 1 if fields["foil"] else 0
    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE collection SET {set_clause} WHERE collection_id=?",
        (*fields.values(), collection_id),
    )
    if old is not None:
        sid, old_qty, old_loc = old
        old_loc = _loc(old_loc)
        new_loc = _loc(fields["location"]) if "location" in fields else old_loc
        new_qty = fields["quantity"] if "quantity" in fields else old_qty
        tracked = old_qty is not None and new_qty is not None
        moved = min(old_qty, new_qty) if tracked else old_qty
        if not tracked or moved > 0:
            _log_collection_move(conn, collection_id, sid, moved, old_loc, new_loc, reason, change_id)
        if tracked and new_qty > old_qty:
            _log_collection_move(conn, collection_id, sid, new_qty - old_qty, None, new_loc,
                                 reason, change_id)
        elif tracked and new_qty < old_qty:
            _log_collection_move(conn, collection_id, sid, old_qty - new_qty, old_loc, None,
                                 reason, change_id)
    conn.commit()


def _find_twin(conn, collection_id):
    """The OTHER lot that is interchangeable with this one — same printing,
    finish, Location (blank == NULL), source and acquired date — or None.
    Lots with no tracked quantity (NULL) never merge: there's nothing to add."""
    row = conn.execute(
        "SELECT scryfall_id, foil, location, date_acquired, source, quantity "
        "FROM collection WHERE collection_id=?", (collection_id,)
    ).fetchone()
    if row is None or row[5] is None:
        return None
    sid, foil, loc, acquired, source, _ = row
    twin = conn.execute(
        """SELECT collection_id FROM collection
           WHERE collection_id != ? AND scryfall_id=? AND foil=?
             AND TRIM(COALESCE(location,'')) = ? AND date_acquired IS ? AND source IS ?
             AND quantity IS NOT NULL
           ORDER BY collection_id LIMIT 1""",
        (collection_id, sid, foil, _loc(loc), acquired, source),
    ).fetchone()
    return twin[0] if twin else None


def _merge_lots(conn, keep_id, drop_id):
    """Fold lot `drop_id` into `keep_id`: quantities add, price_paid adds
    (a NULL side counts as nothing), then `drop_id` is deleted. No move-log
    row and no commit — the copies didn't go anywhere, the caller commits.
    Returns keep_id."""
    keep_q, keep_p = conn.execute(
        "SELECT quantity, price_paid FROM collection WHERE collection_id=?", (keep_id,)).fetchone()
    drop_q, drop_p = conn.execute(
        "SELECT quantity, price_paid FROM collection WHERE collection_id=?", (drop_id,)).fetchone()
    prices = [p for p in (keep_p, drop_p) if p is not None]
    conn.execute(
        "UPDATE collection SET quantity=?, price_paid=? WHERE collection_id=?",
        (keep_q + drop_q, round(sum(prices), 2) if prices else None, keep_id),
    )
    conn.execute("DELETE FROM collection WHERE collection_id=?", (drop_id,))
    return keep_id


def consolidate_lots(conn):
    """One-time (re-runnable) cleanup: merge every group of interchangeable
    lots (see _find_twin) into the lowest-id lot of the group. Returns the
    number of lots removed. Touches no Location, so the move log is untouched."""
    removed = 0
    ids = [r[0] for r in conn.execute("SELECT collection_id FROM collection ORDER BY collection_id")]
    for cid in ids:
        if conn.execute("SELECT 1 FROM collection WHERE collection_id=?", (cid,)).fetchone() is None:
            continue
        while (twin := _find_twin(conn, cid)) is not None:
            # keep the lowest id of the pair
            keep, drop = (cid, twin) if cid < twin else (twin, cid)
            _merge_lots(conn, keep, drop)
            removed += 1
            cid = keep
    conn.commit()
    return removed


def move_lot(conn, collection_id, location, quantity=None, *, reason="collection_edit", change_id=None):
    """Set a lot's Location, moving only `quantity` copies when that is
    fewer than the lot holds (Workbench reconciliation: a lot of 3 where the
    deck needs 1). The remainder stays behind as its own lot at the old
    location, with the same printing/finish/source/date; `price_paid` is
    split in proportion, on the evidence that it's a lot total (a 2-copy lot
    paid $0.69 for a $0.53 card). A NULL-quantity (untracked) lot can't be
    split and always moves whole. If `location` already holds an
    interchangeable lot (_find_twin), the moved copies are merged into it
    rather than left as a second row. Returns the collection_id of the lot
    now at `location`."""
    row = conn.execute(
        "SELECT scryfall_id, quantity, foil, location, date_acquired, price_paid, source "
        "FROM collection WHERE collection_id=?", (collection_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"No collection lot #{collection_id}.")
    sid, have, foil, old_loc, acquired, paid, source = row
    if quantity is None or have is None or quantity >= have:
        update_collection_lot(conn, collection_id, location=location,
                              reason=reason, change_id=change_id)
        twin = _find_twin(conn, collection_id)
        if twin is not None:
            _merge_lots(conn, twin, collection_id)
            conn.commit()
            return twin
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
    # A split is logged against the lot the moved copies now sit in.
    _log_collection_move(conn, cur.lastrowid, sid, quantity, _loc(old_loc), _loc(location),
                         reason, change_id)
    twin = _find_twin(conn, cur.lastrowid)
    if twin is not None:
        _merge_lots(conn, twin, cur.lastrowid)
        conn.commit()
        return twin
    conn.commit()
    return cur.lastrowid


def remove_collection_lot(conn, collection_id, *, reason="collection_edit", change_id=None):
    row = conn.execute(
        "SELECT scryfall_id, quantity, location FROM collection WHERE collection_id=?",
        (collection_id,),
    ).fetchone()
    conn.execute("DELETE FROM collection WHERE collection_id=?", (collection_id,))
    if row is not None:
        _log_collection_move(conn, collection_id, row[0], row[1], _loc(row[2]), None,
                             reason, change_id)
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
        """SELECT col.collection_id, c.name, col.scryfall_id, col.quantity
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
    # Phase 6: a pruned lot leaves a row in the move log (it had no Location,
    # so it reads "no location → deleted") — these used to vanish silently.
    for collection_id, _name, sid, qty in rows:
        _log_collection_move(conn, collection_id, sid, qty, "", None, "prune")
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
# collection quietly drift apart.
#
# No longer called by the Deck Editor (EDIT_FLOW_PLAN.md Phase 1): its add
# bar goes through add_deck_card(), whose sleeve_into_deck() Auto rule moves
# a free copy in before making a starter lot, which this never did. Kept
# because the offline test harness and old lots' "Auto-added (deck build)"
# source refer to it.
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


def bulk_update_collection(conn, updates, *, reason="collection_edit"):
    """updates: {collection_id: {field: value, ..., "_delete": bool}}.
    Returns count changed. Deletes are diffed as unconditional (a row
    flagged _delete is always removed); field updates are diffed against
    current DB state so untouched rows aren't rewritten needlessly. Every
    change reaches the move log through the single-lot writers, as `reason`."""
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
                remove_collection_lot(conn, collection_id, reason=reason)
                changed += 1
            continue
        old = current.get(collection_id, {})
        new_vals = {k: edit.get(k) for k in COLLECTION_FIELDS if k in edit}
        if "foil" in new_vals:
            new_vals["foil"] = 1 if new_vals["foil"] else 0
        if any(old.get(k) != v for k, v in new_vals.items()):
            update_collection_lot(conn, collection_id, reason=reason, **new_vals)
            changed += 1
    return changed


SYNC_NOTE = "following a collection edit"


def sync_deck_lists(conn, offers):
    """Make decks' lists follow a collection-side save: apply the offers
    queries.deck_sync_offers() made (Phase 6's "Also update Gisa's list").

    The lots already moved — the save did that, and logged it — so this
    only changes the LIST, through add_deck_card() / remove_deck_card():
    their move helpers work from what the list needs, find the copies
    already sleeved (or already gone), and move nothing a second time. The
    history row says why the list changed (SYNC_NOTE). An offer whose deck
    has gone is skipped. Returns (lines, moves) for finish_edit() — the
    moves are empty unless something drifted since the offer was made."""
    lines, moves = [], []
    for offer in offers:
        if conn.execute("SELECT 1 FROM decks WHERE deck_id=?", (offer["deck_id"],)).fetchone() is None:
            continue
        sid, new_qty = offer["scryfall_id"], offer["new_quantity"]
        if offer["action"] == "add":
            moves += add_deck_card(conn, offer["deck_id"], sid, new_qty, note=SYNC_NOTE)
            lines.append(f"{offer['deck_name']}: added {offer['card']} ×{offer['quantity']} to the list.")
        elif new_qty <= 0:
            moves += remove_deck_card(conn, offer["deck_id"], sid, note=SYNC_NOTE)
            lines.append(f"{offer['deck_name']}: took {offer['card']} off the list.")
        else:
            moves += add_deck_card(conn, offer["deck_id"], sid, new_qty, note=SYNC_NOTE)
            lines.append(f"{offer['deck_name']}: {offer['card']} down to ×{new_qty} on the list.")
    return lines, moves


# ------------------------------------------------------------------
# Planned buys (EDIT_FLOW_PLAN.md Phase 4). A Buy add puts a card on a list
# with no lot behind it; these two close the loop when the copy turns up.
# ------------------------------------------------------------------
PURCHASE_LOT_SOURCE = "Purchase (buy list)"


def record_purchase(conn, deck_id, scryfall_id, quantity=1, price_paid=None, change_id=None,
                    collection_id=None):
    """A bought copy lands in a deck's sleeves. With `collection_id` (a lot
    that arrived by order import) that many copies move into the deck;
    without, a new lot is made there at `price_paid` per copy (the unit
    price, as an order import records it). Returns the moves.

    `change_id` is the open Shortlist row the purchase answers: a row marked
    Buy goes back to Auto once its copies are bought, so applying it later
    finds them already sleeved here instead of waiting for a copy that now
    exists. (An applied Buy add needs nothing: once a copy is sleeved the
    card stops being `awaiting` by itself.) The row is left alone when the
    purchase covers fewer copies than it wants.

    Refused, writing nothing, for a dismantled deck (its cards are already
    gone) and a basic land (Phase 7: generic, nobody buys one per deck)."""
    deck_name, state = _deck_name_and_state(conn, deck_id)
    if state == "dismantled":
        raise ValueError(f"{deck_name} is dismantled — nothing to sleeve a purchase into.")
    if q.is_basic_land(conn, scryfall_id=scryfall_id):
        raise ValueError("Basic lands are generic — there's no copy to buy or record.")
    quantity = int(quantity)
    if quantity < 1:
        raise ValueError("Bought quantity must be at least 1.")
    if collection_id is not None:
        moves = move_copies(conn, collection_id, deck_name, quantity=quantity, reason="purchase")
    else:
        card = _card_name(conn, scryfall_id) or scryfall_id
        lot_id = add_collection_lot(
            conn, scryfall_id, quantity=quantity, foil=False, location=deck_name,
            date_acquired=datetime.date.today().isoformat(), price_paid=price_paid,
            source=PURCHASE_LOT_SOURCE, reason="purchase", change_id=change_id,
        )
        moves = [_move(card, quantity, "new", lot_id, None, deck_name)]
    if change_id is not None:
        row = get_change(conn, change_id)
        if (row and row["status"] in OPEN_CHANGE_STATUSES and row["source"] == "buy"
                and quantity >= (row["quantity"] or 1)):
            conn.execute("UPDATE deck_changes SET source=NULL, source_location=NULL WHERE change_id=?",
                         (change_id,))
            conn.commit()
    return moves


def sleeve_purchases(conn, offers):
    """Apply the ticked offers of queries.buy_matches() (the order-import
    "Sleeve into Prosper"): each moves copies of an imported lot into the
    deck wanting them. Offers are taken in order and capped by what the lot
    still holds, so two decks ticked for one copy can't both get it — the
    later one is reported, not applied. Returns (lines, moves)."""
    lines, moves = [], []
    for offer in offers:
        lot = conn.execute("SELECT quantity, location FROM collection WHERE collection_id=?",
                           (offer["collection_id"],)).fetchone()
        if lot is None or lot[0] is None:
            lines.append(f"{offer['deck_name']}: {offer['card']} — that lot is gone, skipped.")
            continue
        if (lot[1] or "").strip() != offer.get("from_location", (lot[1] or "").strip()):
            # An earlier ticked offer already sleeved this lot somewhere: its
            # copies are spoken for, even though the lot still shows a quantity.
            lines.append(f"{offer['deck_name']}: {offer['card']} — no copies left in that lot, skipped.")
            continue
        take = min(int(offer["quantity"]), lot[0])
        if take < 1:
            lines.append(f"{offer['deck_name']}: {offer['card']} — no copies left in that lot, skipped.")
            continue
        try:
            moves += record_purchase(conn, offer["deck_id"], offer["scryfall_id"], take,
                                     change_id=offer.get("change_id"), collection_id=offer["collection_id"])
        except ValueError as exc:
            lines.append(f"{offer['deck_name']}: {offer['card']} — {exc}")
            continue
        lines.append(f"{offer['deck_name']}: sleeved {offer['card']} ×{take}.")
    return lines, moves


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
                  add_scryfall_id, add_card_name, quantity=1, default_location=None,
                  reason="deck_edit", change_id=None, source=None, source_location=None):
    """Removes `remove_scryfall_id` from the deck's mainboard and adds
    `add_scryfall_id` in its place at `quantity` copies, then moves the
    copies through the same two helpers as every other deck edit
    (EDIT_FLOW_PLAN.md Phase 1):

      - the cut card's copies sleeved here go to `default_location`
        (DEFAULT_LOCATION, "Box", if not given) — at most as many as the
        list ran, and only those the list no longer needs. (This used to
        move every lot of that NAME in the deck, whatever the quantity.)
      - the added card is sleeved here by sleeve_into_deck(): its Auto
        rule (a free copy in storage if there is one, else a starter lot)
        or, with `source` / `source_location` (Phase 2), a chosen storage
        Location — never one taken out of another deck's sleeves.

    `deck_name` and the two card names are no longer needed (the helpers
    read them by id); the parameters stay so existing callers don't break.
    `reason` / `change_id` go to the move log (Phase 6); apply_change()
    passes "apply" and its row.

    Returns {"moved_out": bool, "assigned_lot_id": int_or_None,
    "created_lot": bool, "moves": [...]}."""
    cut_qty = _deck_card_quantity(conn, deck_id, remove_scryfall_id) or 0
    _delete_deck_card(conn, deck_id, remove_scryfall_id)
    _put_deck_card(conn, deck_id, add_scryfall_id, quantity)
    # Cut first: swapping one printing for another of the same card then
    # finds the copy still needed, and nothing moves either way.
    moves_out = unsleeve_from_deck(conn, deck_id, remove_scryfall_id, cut_qty,
                                   dest=default_location or DEFAULT_LOCATION,
                                   reason=reason, change_id=change_id)
    # The cut card may still have been promised to this deck by another
    # (a brewing deck's take, Phase 3); after both list writes, so swapping
    # one printing for another of it keeps the promise.
    _drop_promises(conn, deck_id, remove_scryfall_id)
    return _move_outcome(moves_out, sleeve_into_deck(conn, deck_id, add_scryfall_id, quantity,
                                                     source=source, source_location=source_location,
                                                     reason=reason, change_id=change_id))



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



# ------------------------------------------------------------------
# Build it (EDIT_FLOW_PLAN.md Phase 5). A brewing deck's edits changed its
# list only; this makes every move it has been waiting on, in one batch:
#
#   1. each lopsided take on another deck (Phase 3: the brew's add is
#      applied, the donor's removal still planned) is applied through the
#      normal apply_change(), so the copy moves donor -> brew
#   2. each listed card still not sleeved here is pulled from the source its
#      last add chose: a storage Location, or Auto (free copies, biggest lot
#      first). Donors go first so step 2 finds the transferred copy already
#      sleeved here instead of making a starter lot for it
#   3. Buy cards stay on the buy list; anything that can't be pulled is
#      listed, never skipped quietly
#
# Auto here NEVER makes a starter lot (unlike a direct edit): a card you
# own no free copy of is reported as "to pull", not recorded as owned.
# ------------------------------------------------------------------
def _card_key(conn, scryfall_id):
    """The oracle-or-lowercased-name key deck_sourcing_plan() groups by."""
    row = conn.execute("SELECT COALESCE(oracle_id, 'name:' || LOWER(name)) FROM cards WHERE scryfall_id=?",
                       (scryfall_id,)).fetchone()
    return row[0] if row else scryfall_id


def _build_run(conn, deck_id, oracle_ids):
    """build_deck()'s work against `conn` (the real database, or the
    throwaway copy a dry run uses). Returns the result dict minus the flags
    build_deck() adds."""
    deck_name, state = _deck_name_and_state(conn, deck_id)
    if state == "dismantled":
        raise ValueError(f"{deck_name} is dismantled — there is nothing to build.")
    if state != "brewing":
        raise ValueError(f"{deck_name} isn't brewing — its edits already moved their copies.")
    wanted = set(oracle_ids) if oracle_ids is not None else None
    moves, pulled, buying, to_pull = [], [], [], []
    failed_donors = {}   # card key -> why its donor couldn't hand the copy over

    def selected(sid):
        return wanted is None or _card_key(conn, sid) in wanted

    # 1. Donors first.
    for take in q.open_takes(conn):
        if (take["receiver_deck_id"] != deck_id or take["receiver_status"] != "applied"
                or take["donor_status"] != "planned" or not selected(take["scryfall_id"])):
            continue
        donor, card, qty = take["donor_deck_name"], take["card"], int(take["quantity"] or 1)
        key = _card_key(conn, take["scryfall_id"])
        try:
            if _deck_name_and_state(conn, take["donor_deck_id"])[1] == "brewing":
                raise ValueError(f"{donor} is itself brewing, so it has no copy of {card} to hand over yet.")
            listed = q.deck_list_quantity(conn, take["donor_deck_id"], take["scryfall_id"])
            sleeved = _sleeved_in(conn, donor, take["scryfall_id"])
            if sleeved - (listed - qty) < qty:
                raise ValueError(
                    f"{donor} has no copy of {card} to hand over — its list runs ×{listed} and only "
                    f"{sleeved} {'is' if sleeved == 1 else 'are'} recorded as sleeved there. Mark it "
                    f"sleeved in {donor} (Workbench → Reconcile) first.")
            outcome = apply_change(conn, take["donor_change_id"])
        except ValueError as exc:
            failed_donors[key] = str(exc)
            continue
        moves += outcome["moves"]
        pulled.append({"card": card, "quantity": qty, "source": f"deck: {donor}", "from": donor,
                       "oracle_id": key})

    # 2. Everything else the list still waits on.
    saved = q.deck_saved_sources(conn, deck_id)
    for sid, entry_qty, card in conn.execute(
        """SELECT dc.scryfall_id, dc.quantity, c.name FROM deck_cards dc
           JOIN cards c ON c.scryfall_id = dc.scryfall_id
           WHERE dc.deck_id=? ORDER BY c.name""", (deck_id,)
    ).fetchall():
        if not selected(sid) or q.is_basic_land(conn, scryfall_id=sid):
            continue
        entry_qty = int(entry_qty or 1)
        needed = min(entry_qty, q.deck_list_quantity(conn, deck_id, sid) - _sleeved_in(conn, deck_name, sid))
        if needed <= 0:
            continue
        source, location = saved.get(sid, (None, None))
        key = _card_key(conn, sid)
        if source == "buy":
            buying.append({"card": card, "quantity": needed, "oracle_id": key})
            continue
        if source == "deck":
            to_pull.append({"card": card, "quantity": needed, "source": f"deck: {location}", "oracle_id": key,
                            "reason": failed_donors.get(key) or
                            f"The take from {location} isn't open any more. Re-plan it, or pick another source."})
            continue
        label = f"storage: {location}" if source == "storage" else "storage"
        try:
            if source == "storage":
                made = sleeve_into_deck(conn, deck_id, sid, entry_qty, source="storage",
                                        source_location=location, force=True, reason="build")
            else:
                free = sum(lot[1] for lot in q.free_lots(conn, sid))
                made = (sleeve_into_deck(conn, deck_id, sid, min(entry_qty, free), force=True, reason="build")
                        if free else [])
        except ValueError as exc:
            to_pull.append({"card": card, "quantity": needed, "source": label, "oracle_id": key,
                            "reason": str(exc)})
            continue
        got = sum(m["quantity"] for m in made if m["kind"] in ("move", "new"))
        moves += made
        if got:
            pulled.append({"card": card, "quantity": got, "source": label, "oracle_id": key,
                           "from": ", ".join(sorted({m["from"] or "no location" for m in made}))})
        if got < needed:
            owned = sum(lot[1] or 1 for lot in q.card_lots(conn, sid))
            to_pull.append({"card": card, "quantity": needed - got, "source": "auto", "oracle_id": key,
                            "reason": "Not owned." if not owned else
                            "No free copy — every owned copy is sleeved in another deck."})
    return {"deck_id": deck_id, "deck_name": deck_name, "moves": moves, "pulled": pulled,
            "buying": buying, "to_pull": to_pull}


def build_deck(conn, deck_id, oracle_ids=None, dry_run=False):
    """"Build it": make the moves a brewing deck has been waiting on (see
    the block comment above). `oracle_ids` limits it to those cards (a
    storage Location's or donor deck's group on the Build Plan), by the key
    deck_sourcing_plan() calls "oracle_id".

    `dry_run=True` writes nothing: the same code runs against a throwaway
    in-memory copy of the database, so the preview lists exactly the moves
    the real run will make.

    Returns {"deck_id", "deck_name", "moves", "pulled", "buying", "to_pull",
    "dry_run", "cleared"}. `to_pull` rows say why a card couldn't be pulled
    ({"card", "quantity", "source", "reason"}); `buying` cards stay on the
    buy list. The brewing flag is cleared (`cleared`; with dry_run, "would
    clear") once nothing is left to pull except Buy cards — so a partial
    build leaves the deck brewing, and a selection that happens to finish
    the job clears it. Raises ValueError for a deck that isn't brewing."""
    if dry_run:
        copy = sqlite3.connect(":memory:")
        try:
            conn.backup(copy)
            copy.row_factory = conn.row_factory
            result = _build_run(copy, deck_id, oracle_ids)
            result["cleared"] = not _left_to_build(copy, deck_id)
        finally:
            copy.close()
        result["dry_run"] = True
        return result
    result = _build_run(conn, deck_id, oracle_ids)
    result["dry_run"] = False
    result["cleared"] = not _left_to_build(conn, deck_id)
    if result["cleared"]:
        set_build_state(conn, deck_id, None)
    logger.info("Built deck_id=%d: %d move(s), %d to pull, %d buying%s", deck_id, len(result["moves"]),
                len(result["to_pull"]), len(result["buying"]), ", flag cleared" if result["cleared"] else "")
    return result


def _left_to_build(conn, deck_id):
    """True while a brewing deck still has an open take on another deck or a
    non-Buy, non-basic card not sleeved here — what keeps it brewing."""
    deck_name = _deck_name_and_state(conn, deck_id)[0]
    if any(t["receiver_deck_id"] == deck_id for t in q.open_takes(conn)):
        return True
    saved = q.deck_saved_sources(conn, deck_id)
    for (sid,) in conn.execute("SELECT scryfall_id FROM deck_cards WHERE deck_id=?", (deck_id,)).fetchall():
        if saved.get(sid, (None, None))[0] == "buy" or q.is_basic_land(conn, scryfall_id=sid):
            continue
        if q.deck_list_quantity(conn, deck_id, sid) - _sleeved_in(conn, deck_name, sid) > 0:
            return True
    return False


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
      - an addition against the target, with source='deck' /
        source_location = this deck (EDIT_FLOW_PLAN.md Phase 3)

    and the two are linked (linked_change_id on both), so applying either
    applies the pair, removal first, and dropping either drops both — the
    same as a take from another deck chosen by hand.

    The removal is still written first so that change_id order — the order
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
        # EDIT_FLOW_PLAN.md Phase 3: the add says where it comes from, and
        # the two rows are linked, so a dismantle transfer reads "from
        # Vilis" and applies / drops as one pair, like a take chosen by hand.
        cur = conn.execute(
            """INSERT INTO deck_changes
                   (deck_id, status, add_scryfall_id, add_name, quantity,
                    notes, planned_at, source, source_location, linked_change_id)
               VALUES (?, 'planned', ?, ?, ?, ?, CURRENT_TIMESTAMP, 'deck', ?, ?)""",
            (plan["target_deck_id"], card["target_scryfall_id"], card["card"],
             card["copies"], notes or f"From {plan['deck_name']}", plan["deck_name"],
             result["removed"][-1]),
        )
        conn.execute("UPDATE deck_changes SET linked_change_id=? WHERE change_id=?",
                     (cur.lastrowid, result["removed"][-1]))
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
    `dest_location` (default "Box"). Returns the moves made (one per lot,
    through move_copies(), so they reach the pop-up like a deck edit's).

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
    moves = []
    for card in plan["strays"]:
        for lot in card["lots"]:
            if allow is not None and lot["collection_id"] not in allow:
                continue
            moves += move_copies(conn, lot["collection_id"], dest_location, reason="dismantle")
    logger.info("Moved %d stray lot(s) out of deck_id=%d to %r", len(moves), deck_id, dest_location)
    return moves

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

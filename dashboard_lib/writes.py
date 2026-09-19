"""
Write layer for in-dashboard editing: card_tags, deck metadata,
deck_cards (mainboard), maybeboard, deck_themes, and the collection
(including prune_collection, the automatic cleanup step run as part of
the Editor page's Card Data "Refresh" action). Plain sqlite3, no
Streamlit dependency, so it can be unit-tested directly against a
scratch copy of mtg_collection.db.

Migration is one-way, CSV -> DB: scripts/migrate.py wipes and rebuilds
the whole database from the CSVs on every run, with no attempt to
preserve anything written here. That's intentional — see migrate.py's
module docstring and the README. Use the CSVs + migrate.py for your
initial import, then manage things here from then on.
"""
import datetime
import sqlite3

from . import queries as q

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


def list_tags(conn, tag_type=None):
    if tag_type:
        rows = conn.execute(
            "SELECT tag_id, tag_type, label FROM tags WHERE tag_type=? ORDER BY label", (tag_type,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT tag_id, tag_type, label FROM tags ORDER BY tag_type, label"
        ).fetchall()
    return [{"tag_id": r[0], "tag_type": r[1], "label": r[2]} for r in rows]


# ------------------------------------------------------------------
# Theme catalog (Phase 2) — the master list the Editor's Themes dropdown
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
# Game Changer category catalog (Phase 2) — the master list the Editor's
# Game Changers dropdown is populated from. Separate from
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
# Mana tag catalog (Prompt Pass 12) — the master list the Editor's Mana
# Tags dropdown is populated from. Separate from mana_tags (the actual
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
# ONLY write path that puts a card in front of the Editor's Mana Tags
# overview table — exact mirror of the Game Changer tag-assignment
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
# (non-deck) storage locations offered in the Editor's Location
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
        return None, f"A deck named '{name}' already exists."
    conn.commit()
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
        return False, f"A deck named '{new_name}' already exists."
    if update_collection_locations:
        conn.execute("UPDATE collection SET location=? WHERE location=?", (new_name, old_name))
    conn.commit()
    return True, None


# Every table scoped to a single deck via a plain deck_id FK — deleted
# outright when the deck is deleted. game_participants is handled
# separately (detached, not deleted — see delete_deck) since a game
# usually still has OTHER decks' results worth keeping.
_DECK_SCOPED_TABLES = (
    "deck_cards", "maybeboard", "card_tags",
    "deck_themes", "deck_win_conditions", "deck_strengths", "deck_weaknesses",
)


def delete_deck(conn, deck_id, clear_collection_locations=True):
    """Permanently removes a deck and everything scoped to it (mainboard,
    maybeboard, card tags, themes, win conditions/strengths/weaknesses).
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
    return deck_name, None


# ------------------------------------------------------------------
# Mainboard (deck_cards)
# ------------------------------------------------------------------
def add_deck_card(conn, deck_id, scryfall_id, quantity=1):
    conn.execute(
        "INSERT OR REPLACE INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,?)",
        (deck_id, scryfall_id, quantity),
    )
    conn.commit()


def remove_deck_card(conn, deck_id, scryfall_id):
    conn.execute("DELETE FROM deck_cards WHERE deck_id=? AND scryfall_id=?", (deck_id, scryfall_id))
    conn.commit()


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
# Maybeboard
# ------------------------------------------------------------------
def add_maybeboard_card(conn, deck_id, scryfall_id, review_flag=0, replace_scryfall_id=None,
                         replace_card_name=None, notes=None):
    conn.execute(
        """INSERT OR REPLACE INTO maybeboard
           (deck_id, scryfall_id, review_flag, replace_scryfall_id, replace_card_name, notes)
           VALUES (?,?,?,?,?,?)""",
        (deck_id, scryfall_id, 1 if review_flag else 0, replace_scryfall_id, replace_card_name, notes),
    )
    conn.commit()


def remove_maybeboard_card(conn, deck_id, scryfall_id):
    conn.execute("DELETE FROM maybeboard WHERE deck_id=? AND scryfall_id=?", (deck_id, scryfall_id))
    conn.commit()


def bulk_update_maybeboard(conn, deck_id, updates):
    """updates: {scryfall_id: {"review_flag": bool, "notes": str,
    "replace_card_name": str, "_delete": bool}}. Returns count changed."""
    current = {
        r[0]: {"review_flag": r[1], "notes": r[2], "replace_card_name": r[3]}
        for r in conn.execute(
            "SELECT scryfall_id, review_flag, notes, replace_card_name FROM maybeboard WHERE deck_id=?",
            (deck_id,),
        ).fetchall()
    }
    changed = 0
    for scryfall_id, edit in updates.items():
        if edit.get("_delete"):
            if scryfall_id in current:
                remove_maybeboard_card(conn, deck_id, scryfall_id)
                changed += 1
            continue
        old = current.get(scryfall_id, {})
        new_review = 1 if edit.get("review_flag") else 0
        new_notes = edit.get("notes") or None
        new_replace = edit.get("replace_card_name") or None
        if (old.get("review_flag") != new_review or old.get("notes") != new_notes
                or old.get("replace_card_name") != new_replace):
            add_maybeboard_card(
                conn, deck_id, scryfall_id, review_flag=new_review,
                replace_card_name=new_replace, notes=new_notes,
            )
            changed += 1
    return changed


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


def remove_collection_lot(conn, collection_id):
    conn.execute("DELETE FROM collection WHERE collection_id=?", (collection_id,))
    conn.commit()


def prune_collection(conn):
    """Deletes any collection lot that meets ALL three conditions:
      a) no assigned location (NULL or blank/whitespace-only)
      b) a quantity of 0 or "none" (i.e. NULL)
      c) not included in any existing deck list — checked against both
         a deck's mainboard (deck_cards) and maybeboard, so a card still
         under consideration for a deck isn't swept away just because it
         isn't physically located anywhere yet.

    Called automatically as part of the Editor page's Card Data "Refresh"
    action (see dashboard_lib/refresh.py) so stale collection rows for
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
             AND col.scryfall_id NOT IN (SELECT scryfall_id FROM maybeboard)
           ORDER BY c.name COLLATE NOCASE"""
    ).fetchall()
    if not rows:
        return []
    ids = [r[0] for r in rows]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM collection WHERE collection_id IN ({placeholders})", ids)
    conn.commit()
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
    return True, None


# ------------------------------------------------------------------
# Deck Building Auto-Add (Prompt Pass 6) — when a card added to a deck's
# mainboard isn't tracked in the collection AT ALL yet, give it a starter
# collection lot automatically instead of letting the deck and the
# collection quietly drift apart. Only called from the Editor's Mainboard
# "Add to mainboard" action (see pages/4_Editor.py) — NOT from the
# Maybeboard add flow, since a maybeboard card is explicitly "under
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
# one queued swap from the Editor's Swap Manager tab. Card resolution
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

    remove_deck_card(conn, deck_id, remove_scryfall_id)
    add_deck_card(conn, deck_id, add_scryfall_id, quantity=quantity)

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

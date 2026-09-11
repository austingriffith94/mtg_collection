"""
Write layer for in-dashboard editing: tags (card_tags/deck_tags), deck
metadata, deck_cards (mainboard), maybeboard, deck_themes, and the
collection. Plain sqlite3, no Streamlit dependency, so it can be
unit-tested directly against a scratch copy of mtg_collection.db.

Migration is one-way, CSV -> DB: scripts/migrate.py wipes and rebuilds
the whole database from the CSVs on every run, with no attempt to
preserve anything written here. That's intentional — see migrate.py's
module docstring and the README. Use the CSVs + migrate.py for your
initial import, then manage things here from then on.
"""
import sqlite3


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
# Deck tags (deck_tags PK is deck_id+tag_id) — no CSV source today, so
# no override tracking needed here; nothing else ever writes this table.
# ------------------------------------------------------------------
def get_deck_tags(conn, deck_id):
    rows = conn.execute(
        """SELECT dt.tag_id, t.tag_type, t.label, dt.notes
           FROM deck_tags dt JOIN tags t ON t.tag_id = dt.tag_id
           WHERE dt.deck_id=? ORDER BY t.tag_type, t.label""",
        (deck_id,),
    ).fetchall()
    return [{"tag_id": r[0], "tag_type": r[1], "label": r[2], "notes": r[3]} for r in rows]


def add_deck_tag(conn, deck_id, tag_type, label, notes=None):
    tag_id = get_or_create_tag(conn, tag_type, label)
    conn.execute(
        "INSERT OR REPLACE INTO deck_tags (deck_id, tag_id, notes) VALUES (?,?,?)",
        (deck_id, tag_id, notes),
    )
    conn.commit()
    return tag_id


def remove_deck_tag(conn, deck_id, tag_id):
    conn.execute("DELETE FROM deck_tags WHERE deck_id=? AND tag_id=?", (deck_id, tag_id))
    conn.commit()


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


def set_deck_active(conn, deck_id, is_active, successor_deck_id=None):
    conn.execute(
        "UPDATE decks SET is_active=?, successor_deck_id=? WHERE deck_id=?",
        (1 if is_active else 0, successor_deck_id if not is_active else None, deck_id),
    )
    conn.commit()


# Every table scoped to a single deck via a plain deck_id FK — deleted
# outright when the deck is deleted. game_participants is handled
# separately (detached, not deleted — see delete_deck) since a game
# usually still has OTHER decks' results worth keeping.
_DECK_SCOPED_TABLES = (
    "deck_cards", "maybeboard", "deck_tags", "card_tags",
    "deck_themes", "deck_win_conditions", "deck_strengths", "deck_weaknesses",
)


def delete_deck(conn, deck_id, clear_collection_locations=True):
    """Permanently removes a deck and everything scoped to it (mainboard,
    maybeboard, card/deck tags, themes, win conditions/strengths/
    weaknesses). Returns (deck_name, error) — error is None on success.

    Two things are deliberately NOT deleted outright, to avoid corrupting
    data that isn't really "this deck's":
      - game_participants rows referencing this deck have their deck_id
        set to NULL rather than being removed — the game (and any OTHER
        decks' results in it) stays in the log, this deck's row just
        reverts to a free-text-only record, the same shape an untracked
        opponent's deck already has.
      - Any OTHER deck whose successor_deck_id pointed at this one has
        that link cleared (set to NULL) rather than being blocked or
        cascaded further.

    collection.location rows that exactly matched this deck's name are
    cleared to NULL by default (clear_collection_locations=True) so
    "sleeved in this deck" tracking doesn't point at a phantom deck —
    set to False to leave that text as-is.
    """
    row = conn.execute("SELECT name FROM decks WHERE deck_id=?", (deck_id,)).fetchone()
    if not row:
        return None, "Deck not found."
    deck_name = row[0]

    conn.execute("UPDATE decks SET successor_deck_id=NULL WHERE successor_deck_id=?", (deck_id,))
    conn.execute("UPDATE game_participants SET deck_id=NULL WHERE deck_id=?", (deck_id,))

    for table in _DECK_SCOPED_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE deck_id=?", (deck_id,))

    if clear_collection_locations:
        conn.execute("UPDATE collection SET location=NULL WHERE location=?", (deck_name,))

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

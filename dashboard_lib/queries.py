"""
Data-access layer: plain sqlite3 + pandas, no Streamlit imports. Every
function takes an open sqlite3.Connection as its first argument, so this
module can be unit-tested directly against mtg_collection.db (or the
offline _test_mtg_collection.db) without launching the dashboard.
"""
import itertools
import os
import sqlite3
import pandas as pd

# Shared column set for anything that joins in a `cards` row. Kept as one
# constant so every card-bearing query (collection, deck_cards, shortlist)
# returns the same shape and can flow through the same rendering helpers.
CARD_COLUMNS = """
    c.scryfall_id, c.name, c.set_code, c.collector_number, c.type_line,
    c.mana_cost, c.cmc, c.color_identity, c.oracle_text, c.rarity, c.image_uri,
    c.local_image_path, c.is_basic_land, c.is_game_changer, c.is_reserved,
    c.is_showcase, c.is_borderless,
    c.commander_legal, c.current_price_usd
"""
# cards.edhrec_salt is no longer selected here as of Prompt Pass 5 — the
# EDHREC Salt Score feature was retired dashboard-wide (see
# PROJECT_STATE.md): no lightweight public API exposes it, and the only
# alternative data source (MTGJSON's edhrecSaltiness field) only ships
# inside its full AllPrintings-scale exports, which are far too heavy an
# ongoing dependency for one hand-sized field in a personal-scale tool.
# The column itself is left in place on cards (additive schema policy —
# see below), so anyone's previously hand-entered values aren't lost, it
# just isn't read into the dashboard anymore.
# oracle_text (Phase 3) feeds dashboard_lib.formatting's MDFC-aware land
# detection and mana-source color classification (has_land_face(),
# classify_mana_colors(), is_mana_rock_or_dork()) — it isn't rendered as
# its own table column anywhere, only consumed by those pure functions.

# Additive, non-destructive schema upgrades for databases built before a
# given column existed. migrate.py is one-way and won't be re-run once
# you've moved to managing things in the dashboard (see its module
# docstring), so this is how the schema evolves from here on instead —
# applied automatically on every connect, never touches existing data,
# and is a no-op once a database is already current.
_SCHEMA_UPGRADES = [
    ("cards", "is_reserved", "ALTER TABLE cards ADD COLUMN is_reserved BOOLEAN DEFAULT 0"),
    # Phase 2 additions:
    ("cards", "is_showcase", "ALTER TABLE cards ADD COLUMN is_showcase BOOLEAN DEFAULT 0"),
    ("cards", "is_borderless", "ALTER TABLE cards ADD COLUMN is_borderless BOOLEAN DEFAULT 0"),
    ("cards", "edhrec_salt", "ALTER TABLE cards ADD COLUMN edhrec_salt REAL"),
    ("decks", "cover_image_path", "ALTER TABLE decks ADD COLUMN cover_image_path TEXT"),
    ("game_participants", "player_name", "ALTER TABLE game_participants ADD COLUMN player_name TEXT"),
    # Phase 6 (deck lifecycle): NULL = a normal built deck, so no existing
    # deck needs a backfill. See BUILD_STATES below for why this is the one
    # piece of the brew/dismantle workflow that is stored rather than derived.
    ("decks", "build_state", "ALTER TABLE decks ADD COLUMN build_state TEXT"),
    # Phase 6: where a removal's physical copy should end up. NULL keeps the
    # long-standing behaviour (back to writes.DEFAULT_LOCATION, "Box"), so
    # every pre-Phase-6 row reads exactly as it always did. A dismantle that
    # feeds another deck sets it to that deck's name, which is what lets a
    # direct transfer move a lot source → target without a stop in the box.
    ("deck_changes", "dest_location", "ALTER TABLE deck_changes ADD COLUMN dest_location TEXT"),
    # EDIT_FLOW_PLAN.md Phase 2: where an ADD's copy comes from. NULL = Auto
    # (writes.sleeve_into_deck()'s rule), 'storage' = a chosen storage
    # location (source_location). 'deck' (Phase 3) and 'buy' (Phase 4) join
    # later. They sit on deck_changes so a staged idea and the applied
    # history row a direct edit writes both carry the choice.
    ("deck_changes", "source", "ALTER TABLE deck_changes ADD COLUMN source TEXT"),
    ("deck_changes", "source_location", "ALTER TABLE deck_changes ADD COLUMN source_location TEXT"),
    # EDIT_FLOW_PLAN.md Phase 3: ties a donor deck's removal to the receiving
    # deck's add when a card is taken from another deck (set on BOTH rows,
    # each naming the other). NULL = not part of a pair, which is every row
    # written before this phase (old dismantle pairs aren't backfilled).
    ("deck_changes", "linked_change_id", "ALTER TABLE deck_changes ADD COLUMN linked_change_id INTEGER"),
]

# The values decks.build_state may hold. NULL (absent from this tuple) is
# the normal case — a deck that is simply built — which is why all twelve
# existing decks need no backfill.
#
# Build *completion* is deliberately NOT stored: deck_reconciliation()
# already derives it as "% of non-basic mainboard physically located in
# this deck", and a derived number self-corrects as cards are sleeved
# where a stored one would rot. But intent is not derivable — a deck at
# 4% located could be a brew in progress or a built deck whose locations
# were never recorded — so intent is the one bit that has to be written
# down, and the only bit that is.
#
# This is not a revival of the is_active/successor_deck_id columns Phase 1
# removed (see _normalize_retired_decks below). Those encoded a permanent
# active/retired axis; this is a transient build-workflow flag that
# "Mark as built" clears back to NULL. Keep it transient or it will
# re-accumulate the same dead state.
BUILD_STATES = ("brewing", "dismantled")

# Additive, non-destructive NEW TABLES for databases built before a given
# table existed (same philosophy as _SCHEMA_UPGRADES above, just for
# whole tables instead of columns — CREATE TABLE IF NOT EXISTS is
# naturally a no-op on a database that already has it). Each entry is
# (table_name, ddl, seed_function_or_None); seed_function runs ONLY the
# first time the table is created on a given database, so a table you've
# since edited (added/removed entries) never gets silently re-seeded.
def _seed_theme_catalog(conn):
    conn.execute(
        """INSERT OR IGNORE INTO theme_catalog (theme)
           SELECT DISTINCT theme FROM deck_themes"""
    )


def _seed_game_changer_category_catalog(conn):
    conn.execute(
        """INSERT OR IGNORE INTO game_changer_category_catalog (category)
           SELECT DISTINCT tag FROM game_changer_tags"""
    )


def _seed_mana_tag_catalog(conn):
    conn.execute(
        """INSERT OR IGNORE INTO mana_tag_catalog (tag)
           SELECT DISTINCT tag FROM mana_tags"""
    )


def _seed_location_catalog(conn):
    """Prompt Pass 13: seed with the two standard locations named in the
    prompt spec, plus whatever non-deck Location text is already in use
    in the collection (so upgrading an existing database doesn't drop
    any of the free-text locations already relied on from the new
    dropdown's options)."""
    conn.execute("INSERT OR IGNORE INTO location_catalog (location) VALUES ('Box')")
    conn.execute("INSERT OR IGNORE INTO location_catalog (location) VALUES ('Lands Box')")
    conn.execute(
        """INSERT OR IGNORE INTO location_catalog (location)
           SELECT DISTINCT location FROM collection
           WHERE location IS NOT NULL AND TRIM(location) != ''
             AND location NOT IN (SELECT name FROM decks)"""
    )


def _seed_deck_changes(conn):
    """Backfill the unified deck_changes lifecycle table (Workbench
    rework, Phase 0 — see docs/archive/IMPLEMENTATION_PLAN.md) from the two tables it
    supersedes, so no existing idea or queued swap has to be retyped:

      maybeboard rows      -> status='idea'
      deck_swap_queue rows -> status='planned'

    A maybeboard row that already names a replacement stays an 'idea'
    rather than being promoted to 'planned': it was never staged for
    execution, and silently moving it into an apply-able queue would be a
    surprise the user never asked for.

    Also creates the covering index here rather than in the DDL above,
    since ensure_schema() executes exactly one statement per table entry.

    Guarded on the table being empty, so this is a no-op if it somehow
    runs twice — change_id is an autoincrement surrogate with no natural
    key, so there's nothing for INSERT OR IGNORE to dedupe on."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deck_changes_deck_status "
        "ON deck_changes(deck_id, status)"
    )
    if conn.execute("SELECT COUNT(*) FROM deck_changes").fetchone()[0]:
        return

    existing = _existing_objects(conn, "table")

    if "maybeboard" in existing:
        conn.execute(
            """INSERT INTO deck_changes (
                   deck_id, status, add_scryfall_id, add_name,
                   remove_scryfall_id, remove_name,
                   quantity, review_flag, notes)
               SELECT mb.deck_id, 'idea', mb.scryfall_id, c.name,
                      mb.replace_scryfall_id, mb.replace_card_name,
                      1, COALESCE(mb.review_flag, 0), mb.notes
               FROM maybeboard mb
               LEFT JOIN cards c ON c.scryfall_id = mb.scryfall_id"""
        )

    if "deck_swap_queue" in existing:
        # queued_at becomes BOTH created_at and planned_at: the old queue
        # had no notion of an unpaired idea, so every row in it was born
        # already planned.
        conn.execute(
            """INSERT INTO deck_changes (
                   deck_id, status, add_name,
                   remove_scryfall_id, remove_name,
                   quantity, created_at, planned_at)
               SELECT sq.deck_id, 'planned', sq.add_name,
                      sq.remove_scryfall_id, sq.remove_name,
                      sq.quantity, sq.queued_at, sq.queued_at
               FROM deck_swap_queue sq"""
        )


_SCHEMA_TABLE_UPGRADES = [
    ("theme_catalog", "CREATE TABLE theme_catalog (theme TEXT PRIMARY KEY)", _seed_theme_catalog),
    (
        "game_changer_category_catalog",
        "CREATE TABLE game_changer_category_catalog (category TEXT PRIMARY KEY)",
        _seed_game_changer_category_catalog,
    ),
    (
        "mana_tag_catalog",
        "CREATE TABLE mana_tag_catalog (tag TEXT PRIMARY KEY)",
        _seed_mana_tag_catalog,
    ),
    (
        "location_catalog",
        "CREATE TABLE location_catalog (location TEXT PRIMARY KEY)",
        _seed_location_catalog,
    ),
    (
        "deck_swap_queue",
        """CREATE TABLE deck_swap_queue (
               queue_id           INTEGER PRIMARY KEY AUTOINCREMENT,
               deck_id            INTEGER NOT NULL REFERENCES decks(deck_id),
               add_name           TEXT NOT NULL,
               remove_scryfall_id TEXT NOT NULL REFERENCES cards(scryfall_id),
               remove_name        TEXT NOT NULL,
               quantity           INTEGER NOT NULL DEFAULT 1,
               queued_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
           )""",
        None,
    ),
    # Unified deck change lifecycle (idea -> planned -> applied/dropped),
    # superseding maybeboard + deck_swap_queue. See schema.sql for the
    # full rationale and _seed_deck_changes above for the backfill.
    (
        "deck_changes",
        """CREATE TABLE deck_changes (
               change_id           INTEGER PRIMARY KEY AUTOINCREMENT,
               deck_id             INTEGER NOT NULL REFERENCES decks(deck_id),
               status              TEXT NOT NULL DEFAULT 'idea'
                                   CHECK(status IN ('idea','planned','applied','dropped')),
               add_scryfall_id     TEXT REFERENCES cards(scryfall_id),
               add_name            TEXT,
               remove_scryfall_id  TEXT REFERENCES cards(scryfall_id),
               remove_name         TEXT,
               quantity            INTEGER NOT NULL DEFAULT 1,
               review_flag         BOOLEAN DEFAULT 0,
               notes               TEXT,
               created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
               planned_at          TIMESTAMP,
               resolved_at         TIMESTAMP,
               dest_location       TEXT,
               source              TEXT,
               source_location     TEXT,
               linked_change_id    INTEGER,
               CHECK(add_scryfall_id IS NOT NULL OR add_name IS NOT NULL
                     OR remove_scryfall_id IS NOT NULL)
           )""",
        _seed_deck_changes,
    ),
    # Commander Spellbook combo cache — see schema.sql for the full
    # rationale and the newline-separated-list convention, and
    # dashboard_lib/spellbook.py for the API details.
    (
        "deck_combos",
        """CREATE TABLE deck_combos (
               deck_id          INTEGER NOT NULL REFERENCES decks(deck_id),
               combo_id         TEXT NOT NULL,
               category         TEXT NOT NULL CHECK(category IN ('included','almost')),
               uses             TEXT,
               requires         TEXT,
               missing          TEXT,
               produces         TEXT,
               description      TEXT,
               prerequisites    TEXT,
               mana_needed      TEXT,
               popularity       INTEGER,
               bracket_tag      TEXT,
               commander_legal  BOOLEAN DEFAULT 1,
               PRIMARY KEY (deck_id, combo_id, category)
           )""",
        None,
    ),
    (
        "deck_combo_sync",
        """CREATE TABLE deck_combo_sync (
               deck_id        INTEGER PRIMARY KEY REFERENCES decks(deck_id),
               fetched_at     TIMESTAMP,
               identity       TEXT,
               included_count INTEGER,
               almost_count   INTEGER
           )""",
        None,
    ),
    # Move log (EDIT_FLOW_PLAN.md Phase 6) — see schema.sql for the column
    # meanings. Starts empty: moves made before this existed aren't known.
    (
        "collection_moves",
        """CREATE TABLE collection_moves (
               move_id         INTEGER PRIMARY KEY AUTOINCREMENT,
               collection_id   INTEGER,
               scryfall_id     TEXT,
               card_name       TEXT,
               quantity        INTEGER,
               from_location   TEXT,
               to_location     TEXT,
               reason          TEXT NOT NULL,
               change_id       INTEGER,
               moved_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
           )""",
        None,
    ),
]

# Same additive philosophy as _SCHEMA_TABLE_UPGRADES, but for VIEWS (kept
# separate since sqlite_master distinguishes type='table' from
# type='view', and a view has no rows to seed).
_SCHEMA_VIEW_UPGRADES = [
    (
        "player_stats",
        """CREATE VIEW player_stats AS
           SELECT
               gp.player_name AS player_name,
               COUNT(*) AS games_played,
               SUM(CASE WHEN gp.is_winner THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN NOT gp.is_winner THEN 1 ELSE 0 END) AS losses,
               ROUND(1.0 * SUM(CASE WHEN gp.is_winner THEN 1 ELSE 0 END)
                     / NULLIF(COUNT(*), 0), 3) AS win_rate
           FROM game_participants gp
           WHERE gp.player_name IS NOT NULL AND TRIM(gp.player_name) != ''
           GROUP BY gp.player_name""",
    ),
]


def _normalize_retired_decks(conn):
    """Phase 1 removed the Retired Decks feature — the app now treats
    every tracked deck as active, full stop. Databases built before this
    change may still physically carry the old `is_active`/
    `successor_deck_id` columns (dropping columns from a live SQLite file
    is intentionally avoided here, in keeping with this project's
    additive/non-destructive schema-upgrade policy — see _SCHEMA_UPGRADES
    above). Rather than leave stale "retired" data lying around for code
    that no longer looks at it, this normalizes those columns in place —
    every deck becomes active and any successor link is cleared — which
    is idempotent and a no-op after the first run on a given database."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(decks)").fetchall()}
    changed = False
    if "is_active" in cols:
        conn.execute("UPDATE decks SET is_active = 1 WHERE is_active != 1")
        changed = True
    if "successor_deck_id" in cols:
        conn.execute("UPDATE decks SET successor_deck_id = NULL WHERE successor_deck_id IS NOT NULL")
        changed = True
    if changed:
        conn.commit()


def _existing_objects(conn, kind):
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type=?", (kind,)
        ).fetchall()
    }


def ensure_schema(conn):
    # New TABLES before new COLUMNS, because a column upgrade may target a
    # table that is itself an upgrade: deck_changes.dest_location (Phase 6)
    # is added to a table that only exists from Phase 0 onwards, and on a
    # database older than that the ALTER would hit "no such table". Running
    # the table pass first means the column pass always sees every table.
    # No seed function reads a column added below, so the order is free.
    existing_tables = _existing_objects(conn, "table")
    for table, ddl, seed_fn in _SCHEMA_TABLE_UPGRADES:
        if table not in existing_tables:
            conn.execute(ddl)
            conn.commit()
            if seed_fn:
                seed_fn(conn)
                conn.commit()

    for table, column, ddl in _SCHEMA_UPGRADES:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(ddl)
            conn.commit()

    existing_views = _existing_objects(conn, "view")
    for view, ddl in _SCHEMA_VIEW_UPGRADES:
        if view not in existing_views:
            conn.execute(ddl)
            conn.commit()

    _normalize_retired_decks(conn)


def open_connection(db_path):
    """Open a new sqlite3 connection to `db_path` and apply any pending
    schema upgrades (see ensure_schema above). Named distinctly from
    db.get_connection() — that's the Streamlit-cached wrapper around
    this that the app actually calls; this one opens a fresh connection
    every time and is for callers outside the cached app session
    (scripts/refresh_card_data.py) or db.py itself."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    return conn


# ------------------------------------------------------------------
# Decks
# ------------------------------------------------------------------
def list_decks(conn):
    return pd.read_sql_query(
        """SELECT deck_id, name, commander, partner, representative,
                  color_identity, deck_type, build_state
           FROM decks
           ORDER BY name COLLATE NOCASE""",
        conn,
    )


def deck_meta(conn, deck_id):
    row = conn.execute("SELECT * FROM decks WHERE deck_id = ?", (deck_id,)).fetchone()
    return dict(row) if row else {}


def list_decks_with_covers(conn):
    """Same deck list as list_decks(), plus everything the home-page
    landing grid (Prompt Pass 4) needs to pick a thumbnail per deck: the
    user-set cover_image_path if any, and — as a fallback for decks
    without one — the commander's own card art, resolved by matching
    decks.commander against the deck's own deck_cards/cards rows (not a
    global name lookup) so a commander owned across multiple printings
    still resolves to the specific printing actually run in that deck.
    MAX() is used purely to collapse the one-to-many deck_cards join
    down to a single row per deck; every non-aggregated deck_cards row
    besides the commander's own naturally has NULL for the two
    commander_* columns being maxed, so this doesn't average or pick
    among several candidates."""
    return pd.read_sql_query(
        """SELECT d.deck_id, d.name, d.representative, d.commander,
                  d.cover_image_path,
                  MAX(c.image_uri) AS commander_image_uri,
                  MAX(c.local_image_path) AS commander_local_image_path
           FROM decks d
           LEFT JOIN deck_cards dc ON dc.deck_id = d.deck_id
           LEFT JOIN cards c ON c.scryfall_id = dc.scryfall_id AND c.name = d.commander
           GROUP BY d.deck_id
           ORDER BY d.name COLLATE NOCASE""",
        conn,
    )


def cards_by_name(conn, names):
    """Look up cards.* rows (mana_cost/cmc/color_identity/type_line/
    oracle_text) by exact, case-insensitive name — used by the Commander
    Cast Probability engine (Phase 3) to find the commander/partner's own
    card data, since the commander itself is excluded from
    deck_library_dataframe (it lives in the command zone, never in the
    shuffled library). Grouped by name so a commander owned across
    multiple printings still returns one row."""
    names = [n for n in (names or []) if n]
    if not names:
        return pd.DataFrame(columns=["name", "mana_cost", "cmc", "color_identity", "type_line", "oracle_text"])
    placeholders = ",".join("?" for _ in names)
    return pd.read_sql_query(
        f"""SELECT name, mana_cost, cmc, color_identity, type_line, oracle_text
            FROM cards WHERE name COLLATE NOCASE IN ({placeholders})
            GROUP BY name""",
        conn,
        params=names,
    )


# ------------------------------------------------------------------
# Card name autocomplete + printing lookup (Prompt Pass 6) — powers the
# Editor's Collection tab "add a card" flow: search_card_names() drives
# the type-ahead suggestion list, card_printings_by_name() drives the
# second "choose a printing" selector once a name is picked. Both query
# the local `cards` table only (the dashboard's own "master card
# registry" — no network needed); card_resolver.fetch_additional_printings()
# is the explicit, user-initiated way to extend that registry with
# printings Scryfall knows about but this database doesn't yet.
# ------------------------------------------------------------------
def search_card_names(conn, query_text, limit=25):
    """Distinct card names from `cards` matching `query_text` as a
    case-insensitive substring, names that START WITH the query ranked
    ahead of names that merely contain it, alphabetical within each
    group. Returns [] for blank input rather than the whole table.
    Called directly from pages rather than through a loaders.py
    @st.cache_data wrapper — it's a search-as-you-type lookup with a
    new `query_text` on nearly every keystroke, so caching it would add
    overhead without saving repeat work."""
    query_text = (query_text or "").strip()
    if not query_text:
        return []
    contains_pattern = f"%{query_text}%"
    starts_pattern = f"{query_text}%"
    rows = conn.execute(
        """SELECT DISTINCT name FROM cards
           WHERE name LIKE ? COLLATE NOCASE
           ORDER BY CASE WHEN name LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END,
                    name COLLATE NOCASE
           LIMIT ?""",
        (contains_pattern, starts_pattern, limit),
    ).fetchall()
    return [r[0] for r in rows]


_PRINTING_COLUMNS = [
    "scryfall_id", "name", "set_code", "collector_number", "rarity",
    "current_price_usd", "image_uri", "local_image_path",
]


def card_printings_by_name(conn, name):
    """Every printing of `name` already known locally (one dict per
    `cards` row: scryfall_id/set_code/collector_number/rarity/
    current_price_usd/image_uri/local_image_path), exact case-insensitive
    name match, ordered by set then collector number. Returns [] if the
    name isn't in the local database under any printing yet. Builds dicts
    from plain tuples (not sqlite3.Row) so this works regardless of the
    caller's row_factory setting. Called directly from pages rather than
    through a loaders.py @st.cache_data wrapper — it's a one-off lookup
    driven by the user's current name selection, not a repeated query
    worth caching."""
    name = (name or "").strip()
    if not name:
        return []
    rows = conn.execute(
        f"""SELECT {", ".join(_PRINTING_COLUMNS)}
           FROM cards WHERE name = ? COLLATE NOCASE
           ORDER BY set_code COLLATE NOCASE, collector_number COLLATE NOCASE""",
        (name,),
    ).fetchall()
    return [dict(zip(_PRINTING_COLUMNS, r)) for r in rows]


def deck_stats_row(conn, deck_id):
    row = conn.execute(
        "SELECT games_played, wins, losses, win_rate FROM deck_stats WHERE deck_id = ?",
        (deck_id,),
    ).fetchone()
    if not row:
        return {"games_played": 0, "wins": 0, "losses": 0, "win_rate": None}
    d = dict(row)
    d["games_played"] = d["games_played"] or 0
    d["wins"] = d["wins"] or 0
    d["losses"] = d["losses"] or 0
    return d


def deck_value_row(conn, deck_id):
    row = conn.execute(
        "SELECT total_cards, total_value FROM deck_value WHERE deck_id = ?",
        (deck_id,),
    ).fetchone()
    if not row:
        return {"total_cards": 0, "total_value": 0.0}
    d = dict(row)
    d["total_cards"] = d["total_cards"] or 0
    d["total_value"] = d["total_value"] or 0.0
    return d


def in_deck_sleeved_count(conn, deck_id, deck_name):
    """SUM(collection.quantity) where Location matches this deck's name
    exactly (physically sleeved cards, per the Location free-text
    convention), PLUS the deck list's own basic land quantities.

    Basic lands are never given their own collection rows (per the
    project's Location-tagging convention — see PROJECT_STATE.md), even
    though they're physically sleeved in the deck along with everything
    else, so counting collection rows alone always undercounts a deck
    by however many basics it runs. cards.is_basic_land (populated by
    scryfall_lookup.py from the type line) lets us add those in without
    needing an explicit collection entry for them.

    Returns None if nothing matched on either side (not necessarily
    zero — could just mean nothing in the collection is tagged with
    that location and the deck list itself hasn't loaded yet)."""
    collection_row = conn.execute(
        "SELECT SUM(quantity) FROM collection WHERE location = ?", (deck_name,)
    ).fetchone()
    collection_sleeved = collection_row[0]

    basic_row = conn.execute(
        """SELECT SUM(dc.quantity)
           FROM deck_cards dc
           JOIN cards c ON c.scryfall_id = dc.scryfall_id
           WHERE dc.deck_id = ? AND c.is_basic_land = 1""",
        (deck_id,),
    ).fetchone()
    basic_lands = basic_row[0] or 0

    if collection_sleeved is None and basic_lands == 0:
        return None
    return (collection_sleeved or 0) + basic_lands


_RANK_TABLES = {"win_conditions", "strengths", "weaknesses"}


def deck_rank_list(conn, deck_id, kind):
    if kind not in _RANK_TABLES:
        raise ValueError(f"Unknown rank list kind: {kind!r}")
    table = f"deck_{kind}"
    rows = conn.execute(
        f"SELECT rank, label, description FROM {table} WHERE deck_id = ? ORDER BY rank",
        (deck_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def deck_themes_list(conn, deck_id):
    rows = conn.execute(
        "SELECT theme, role FROM deck_themes WHERE deck_id = ? ORDER BY role, theme",
        (deck_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def deck_mana_tag_summary(conn, deck_id):
    rows = conn.execute(
        "SELECT tag, cards FROM deck_mana_tag_summary WHERE deck_id = ? ORDER BY tag",
        (deck_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def deck_game_changers(conn, deck_id):
    df = pd.read_sql_query(
        """SELECT card_name, scryfall_flag, custom_tag
           FROM deck_game_changers WHERE deck_id = ? ORDER BY card_name""",
        conn,
        params=(deck_id,),
    )
    if df.empty:
        return df

    def status(r):
        if r["scryfall_flag"] and r["custom_tag"]:
            return "Confirmed"
        if r["scryfall_flag"] and not r["custom_tag"]:
            return "Scryfall-only (add a category tag)"
        return "Your list only (Scryfall disagrees)"

    df["status"] = df.apply(status, axis=1)
    return df


def deck_price_top10(conn, deck_id):
    """Top 10 most expensive mainboard cards (by current_price_usd,
    single-copy unit pricing — quantity is intentionally not shown/used
    here), priced cards only, ties broken alphabetically. Also surfaces
    what was actually paid for this card (collection.price_paid, summed
    across any lot(s) whose location matches this deck's name, i.e. the
    copy(ies) actually sleeved in it) as `purchase_price` — NULL when
    nothing's been logged as physically sleeved here."""
    return pd.read_sql_query(
        """SELECT c.name AS card_name, c.current_price_usd AS price,
                  (SELECT SUM(col.price_paid) FROM collection col
                    WHERE col.scryfall_id = c.scryfall_id AND col.location = d.name
                  ) AS purchase_price
           FROM deck_cards dc
           JOIN cards c ON c.scryfall_id = dc.scryfall_id
           JOIN decks d ON d.deck_id = dc.deck_id
           WHERE dc.deck_id = ? AND c.current_price_usd IS NOT NULL
           ORDER BY c.current_price_usd DESC, c.name COLLATE NOCASE
           LIMIT 10""",
        conn,
        params=(deck_id,),
    )


# deck_salt_top10() (Top 10 Saltiest cards) was removed in Prompt Pass 5
# along with the rest of the EDHREC Salt Score feature — see the
# CARD_COLUMNS comment above and PROJECT_STATE.md for why.


def deck_reserved_list_cards(conn, deck_id):
    """Mainboard cards in this deck that are on Scryfall's live Reserved
    List, with the card's current price (rather than quantity run) —
    what a Reserved List card is worth is usually more actionable at a
    glance than how many copies are in a singleton Commander deck."""
    return pd.read_sql_query(
        """SELECT c.name AS card_name, c.current_price_usd AS price
           FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
           WHERE dc.deck_id = ? AND c.is_reserved = 1
           ORDER BY c.name""",
        conn,
        params=(deck_id,),
    )


def deck_strategy_tag_counts(conn, deck_id):
    return pd.read_sql_query(
        """SELECT t.label AS tag, COUNT(*) AS card_count, ROUND(AVG(c.cmc), 2) AS avg_cmc
           FROM card_tags ct
           JOIN tags t ON t.tag_id = ct.tag_id
           JOIN cards c ON c.scryfall_id = ct.scryfall_id
           WHERE ct.deck_id = ? AND t.tag_type = 'strategy'
           GROUP BY t.label
           ORDER BY card_count DESC""",
        conn,
        params=(deck_id,),
    )


def deck_mana_curve(conn, deck_id):
    """CMC histogram, non-land cards only (standard convention)."""
    return pd.read_sql_query(
        f"""SELECT c.cmc AS cmc, SUM(dc.quantity) AS count
            FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
            WHERE dc.deck_id = ? AND c.type_line NOT LIKE '%Land%'
            GROUP BY c.cmc ORDER BY c.cmc""",
        conn,
        params=(deck_id,),
    )


# ------------------------------------------------------------------
# Combos (Commander Spellbook) — reads against the deck_combos /
# deck_combo_sync cache. The fetching itself lives in
# dashboard_lib/spellbook.py (network) and writes.save_deck_combos (cache
# population); nothing here touches the network.
# ------------------------------------------------------------------
def deck_combo_card_rows(conn, deck_id):
    """The deck's mainboard as [(card_name, quantity), ...] — the input
    shape spellbook.build_deck_payload() expects. Plain rows rather than a
    DataFrame since the payload builder just iterates them."""
    return [
        (row[0], row[1])
        for row in conn.execute(
            """SELECT c.name, dc.quantity
               FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
               WHERE dc.deck_id = ?
               ORDER BY c.name COLLATE NOCASE""",
            (deck_id,),
        ).fetchall()
    ]


def deck_combos(conn, deck_id, category):
    """Cached combos for one deck, most-popular-first. `category` is
    'included' or 'almost' (spellbook.CATEGORY_*). The newline-separated
    list columns are returned as raw text — the page splits them, since
    how they're rendered (bullets, chips, one line) differs per column."""
    return pd.read_sql_query(
        """SELECT combo_id, uses, requires, missing, produces, description,
                  prerequisites, mana_needed, popularity, bracket_tag,
                  commander_legal
           FROM deck_combos
           WHERE deck_id = ? AND category = ?
           ORDER BY COALESCE(popularity, -1) DESC, combo_id""",
        conn,
        params=(deck_id, category),
    )


def deck_combo_sync(conn, deck_id):
    """When this deck's combos were last fetched, and what was found.
    Returns None if the deck has NEVER been synced — which is what lets the
    UI say "not fetched yet" instead of wrongly claiming a deck has no
    combos (zero rows in deck_combos means both things on its own).

    `stale` (Phase 0) is True once the deck's latest applied change
    (including a logged quantity bump) is dated after this fetch — the
    decklist Spellbook last saw may no longer be the current one."""
    row = conn.execute(
        """SELECT deck_id, fetched_at, identity, included_count, almost_count
           FROM deck_combo_sync WHERE deck_id = ?""",
        (deck_id,),
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    last_edit = conn.execute(
        "SELECT MAX(resolved_at) FROM deck_changes WHERE deck_id = ? AND status = 'applied'",
        (deck_id,),
    ).fetchone()[0]
    result["stale"] = bool(last_edit and last_edit > result["fetched_at"])
    return result


def deck_combo_sync_all(conn):
    """Sync state for every deck at once, for the Card Database page's
    bulk-refresh summary table. LEFT JOIN so never-synced decks are listed
    too (with NULL counts) rather than silently missing from the overview."""
    return pd.read_sql_query(
        """SELECT d.deck_id, d.name AS deck_name, s.fetched_at,
                  s.included_count, s.almost_count
           FROM decks d
           LEFT JOIN deck_combo_sync s ON s.deck_id = d.deck_id
           ORDER BY d.name COLLATE NOCASE""",
        conn,
    )


def owned_card_quantities(conn):
    """Collection availability for EVERY owned card, keyed by the same
    front-face-lowercase key spellbook.front_face() produces:
        {key: {"name": str, "owned_qty": int, "available_qty": int}}

    Powers the "you already own the missing card" flag on the one-card-away
    combo list. Built as one query + a Python grouping rather than a
    per-card lookup because a single deck can be one card away from 150+
    combos — card_inventory_status() is the right tool for one card at a
    time (the Swap Manager), not for 150.

    `available_qty` excludes copies sleeved in a tracked deck (same rule as
    card_inventory_status: a Location matching a deck's name means it's in
    that deck), so a card you own but have already committed elsewhere
    doesn't read as "just add it".

    NULL quantity means untracked/bulk (basic lands) — counted as 1 so
    those don't look unowned.
    """
    deck_names = {r[0] for r in conn.execute("SELECT name FROM decks").fetchall()}

    out = {}
    rows = conn.execute(
        """SELECT c.name, col.quantity, col.location
           FROM collection col JOIN cards c ON c.scryfall_id = col.scryfall_id"""
    ).fetchall()

    for name, quantity, location in rows:
        key = (name or "").split(" // ")[0].strip().lower()
        if not key:
            continue
        qty = 1 if quantity is None else quantity
        entry = out.setdefault(key, {"name": name, "owned_qty": 0, "available_qty": 0})
        entry["owned_qty"] += qty
        if (location or "").strip() not in deck_names:
            entry["available_qty"] += qty
    return out


def deck_cards_dataframe(conn, deck_id):
    """One row per mainboard card, with quantity and any strategy tag(s)
    (comma-joined — schema supports multiple tags per card-in-deck even
    though today's data only populates one)."""
    return pd.read_sql_query(
        f"""SELECT {CARD_COLUMNS}, dc.quantity,
                   GROUP_CONCAT(DISTINCT t.label) AS strategy_tags
            FROM deck_cards dc
            JOIN cards c ON c.scryfall_id = dc.scryfall_id
            LEFT JOIN card_tags ct
                   ON ct.deck_id = dc.deck_id AND ct.scryfall_id = dc.scryfall_id
            LEFT JOIN tags t ON t.tag_id = ct.tag_id AND t.tag_type = 'strategy'
            WHERE dc.deck_id = ?
            GROUP BY c.scryfall_id
            ORDER BY c.name COLLATE NOCASE""",
        conn,
        params=(deck_id,),
    )


def deck_library_dataframe(conn, deck_id):
    """The actual shuffled-library pool used for draw-probability math:
    the mainboard MINUS the commander (and partner, if any) — those sit in
    the command zone and are never part of the shuffled 99(ish)-card deck.
    Matched by exact name against decks.commander / decks.partner."""
    meta = deck_meta(conn, deck_id)
    df = deck_cards_dataframe(conn, deck_id)
    commander_names = {n for n in (meta.get("commander"), meta.get("partner")) if n}
    if commander_names:
        df = df[~df["name"].isin(commander_names)].reset_index(drop=True)
    return df


_CHANGE_STATUS_ORDER = ("idea", "planned", "applied", "dropped")


def deck_changes_dataframe(conn, deck_id, statuses=("idea", "planned")):
    """Rows of the deck_changes lifecycle table for one deck, for the Deck
    Editor's Shortlist tab. Both card sides are LEFT-joined because either
    may be unlinked: an idea can be unpaired (no remove side), and every
    swap backfilled from the old queue stored only a typed name for the add
    side. `add_card`/`remove_card` therefore prefer the linked printing's
    real name and fall back to the stored text — the text is often
    lowercase (`credit voucher`) even when it resolves fine.

    `price` falls back to the cheapest known printing of the same name for
    an unlinked add side, so an unowned idea still shows what it'd cost.

    Sorted by card name; callers that want planned-order or newest-first
    (the Planned and History sections) re-sort on the timestamp columns."""
    statuses = tuple(statuses)
    bad = [x for x in statuses if x not in _CHANGE_STATUS_ORDER]
    if bad:
        raise ValueError(f"Unknown change status: {bad!r}")
    if not statuses:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in statuses)
    return pd.read_sql_query(
        f"""SELECT dc.change_id, dc.deck_id, dc.status, dc.quantity, dc.review_flag, dc.notes,
                   dc.created_at, dc.planned_at, dc.resolved_at,
                   dc.source, dc.source_location, dc.dest_location,
                   dc.linked_change_id, lk.status AS linked_status, ld.name AS linked_deck_name,
                   dc.add_scryfall_id,    COALESCE(ac.name, dc.add_name)    AS add_card,
                   dc.remove_scryfall_id, COALESCE(rc.name, dc.remove_name) AS remove_card,
                   COALESCE(ac.current_price_usd,
                            (SELECT MIN(p.current_price_usd) FROM cards p
                             WHERE p.name = dc.add_name COLLATE NOCASE)) AS price
            FROM deck_changes dc
            LEFT JOIN cards ac ON ac.scryfall_id = dc.add_scryfall_id
            LEFT JOIN cards rc ON rc.scryfall_id = dc.remove_scryfall_id
            LEFT JOIN deck_changes lk ON lk.change_id = dc.linked_change_id
            LEFT JOIN decks ld ON ld.deck_id = lk.deck_id
            WHERE dc.deck_id = ? AND dc.status IN ({placeholders})
            ORDER BY COALESCE(ac.name, dc.add_name, dc.remove_name) COLLATE NOCASE, dc.change_id""",
        conn,
        params=(deck_id, *statuses),
    )


def open_changes_dataframe(conn, statuses=("planned",), deck_id=None):
    """Every deck's deck_changes rows in the given statuses, carrying the
    deck name — the Workbench's global queue (Phase 2), where
    deck_changes_dataframe() is deliberately one-deck-at-a-time for the
    Deck Editor.

    Same column names as deck_changes_dataframe() plus `deck_name` and
    `oracle_id`, so a row from either source can flow through
    plan_availability() and the same row-table rendering. Ordered the way
    the allocator walks them (planned_at, then change_id) so a page's table
    and the verdicts beside it are in one order.

    `deck_id` narrows it to one deck without changing the shape, so one
    deck's verdicts come out of exactly the same code path as the global
    ones and the two views can't disagree."""
    statuses = tuple(statuses)
    bad = [x for x in statuses if x not in _CHANGE_STATUS_ORDER]
    if bad:
        raise ValueError(f"Unknown change status: {bad!r}")
    if not statuses:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in statuses)
    deck_filter = "AND dc.deck_id = ?" if deck_id is not None else ""
    params = [*statuses] + ([deck_id] if deck_id is not None else [])
    return pd.read_sql_query(
        f"""SELECT dc.change_id, dc.deck_id, d.name AS deck_name, dc.status,
                   dc.quantity, dc.review_flag, dc.notes,
                   dc.created_at, dc.planned_at, dc.resolved_at,
                   dc.source, dc.source_location, dc.dest_location,
                   dc.linked_change_id, lk.status AS linked_status,
                   lk.deck_id AS linked_deck_id, ld.name AS linked_deck_name,
                   dc.add_scryfall_id,    COALESCE(ac.name, dc.add_name)    AS add_card,
                   dc.remove_scryfall_id, COALESCE(rc.name, dc.remove_name) AS remove_card,
                   ac.oracle_id,
                   COALESCE(ac.current_price_usd,
                            (SELECT MIN(p.current_price_usd) FROM cards p
                             WHERE p.name = dc.add_name COLLATE NOCASE)) AS price
            FROM deck_changes dc
            JOIN decks d ON d.deck_id = dc.deck_id
            LEFT JOIN cards ac ON ac.scryfall_id = dc.add_scryfall_id
            LEFT JOIN cards rc ON rc.scryfall_id = dc.remove_scryfall_id
            LEFT JOIN deck_changes lk ON lk.change_id = dc.linked_change_id
            LEFT JOIN decks ld ON ld.deck_id = lk.deck_id
            WHERE dc.status IN ({placeholders}) {deck_filter}
            ORDER BY COALESCE(dc.planned_at, dc.created_at), dc.change_id""",
        conn,
        params=params,
    )


def shortlist_dataframe(conn, deck_id):
    """A deck's open shortlist (ideas AND staged swaps) in the shared
    card-browser shape — CARD_COLUMNS plus status/review/replace/notes — for
    the Decks page's Shortlist board. This is what maybeboard_dataframe was.

    INNER join on the add card, so a row not yet linked to a printing is not
    shown here: the browser needs a real card (image, type, set) to render a
    tile. That is every swap backfilled from the old queue; they still appear
    on the Deck Editor's Shortlist tab and link up when applied.

    `replace_id` (the cut's scryfall_id) lets the Board badge the mainboard
    card a planned swap would cut; `quantity` is the row's own copy count."""
    return pd.read_sql_query(
        f"""SELECT {CARD_COLUMNS}, dc.change_id, dc.status, dc.review_flag,
                   COALESCE(rc.name, dc.remove_name) AS replace_target,
                   dc.remove_scryfall_id AS replace_id, dc.quantity,
                   dc.notes, dc.source, dc.source_location, dc.dest_location
            FROM deck_changes dc
            JOIN cards c ON c.scryfall_id = dc.add_scryfall_id
            LEFT JOIN cards rc ON rc.scryfall_id = dc.remove_scryfall_id
            WHERE dc.deck_id = ? AND dc.status IN ('idea', 'planned')
            ORDER BY c.name COLLATE NOCASE""",
        conn,
        params=(deck_id,),
    )


# ------------------------------------------------------------------
# Collection Location dropdown + Swap Manager inventory checking
# (Prompt Pass 13 / prompt6.txt)
# ------------------------------------------------------------------
def location_options(conn):
    """Every valid Location value offered in the Editor's dropdowns: the
    standard-location catalog (location_catalog — "Box", "Lands Box",
    etc.) unioned with every currently tracked deck's own name (a deck
    name in collection.location has always meant "sleeved in that deck",
    per this project's free-text convention — see schema.sql's comment
    on collection.location). Sorted case-insensitively."""
    catalog = {r[0] for r in conn.execute("SELECT location FROM location_catalog").fetchall()}
    decks = {r[0] for r in conn.execute("SELECT name FROM decks").fetchall()}
    return sorted(catalog | decks, key=str.casefold)


def deck_name_set(conn):
    """Every tracked deck's name. A collection lot whose Location exactly
    matches one of these means "sleeved in that deck" (this project's
    free-text convention — see schema.sql on collection.location);
    anything else, including blank, is in storage and available."""
    return {r[0] for r in conn.execute("SELECT name FROM decks").fetchall()}


def oracle_ids_for_card(conn, name=None, scryfall_id=None):
    """The oracle_id(s) a card reference resolves to — the join key for
    "do I own this card", as opposed to "do I own this printing".

    A printing's id resolves through `cards`; a bare name resolves to every
    oracle_id carried by a card of that name, because a reference may be a
    typed name with no printing linked to it yet (every swap backfilled
    from the old queue is one).

    Returns an empty set when nothing matches OR when the matching rows
    have a NULL oracle_id — a card added before oracle_id was synced. The
    caller must then fall back to matching on name; that is what
    _lots_for_card() below does, and why it takes both."""
    ids = set()
    if scryfall_id:
        row = conn.execute("SELECT oracle_id FROM cards WHERE scryfall_id=?", (scryfall_id,)).fetchone()
        if row and row[0]:
            ids.add(row[0])
    if not ids and name:
        ids = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT oracle_id FROM cards WHERE name = ? COLLATE NOCASE AND oracle_id IS NOT NULL",
                (name.strip(),),
            ).fetchall()
        }
    return ids


def _lots_for_card(conn, name=None, scryfall_id=None):
    """Collection lots of a card, across every printing of it:
    [(collection_id, quantity, location), ...].

    Matched on oracle_id, falling back to an exact case-insensitive name
    match only when no oracle_id is available. Matching by NAME alone was
    the long-standing bug this replaces: a modal double-faced card is
    stored under its full `Front // Back` name, so a reference to either
    face missed every copy you own, and two printings of one card that
    differ in name punctuation missed each other too.

    `quantity` comes back COALESCEd to 1. collection.quantity is nullable
    and NULL means "untracked / bulk" (see schema.sql), not zero — 183 of
    the 1,472 lots in the live database are NULL. The per-row check this
    feeds used to read that as `quantity or 0` and report a card sitting
    in a box as NOT OWNED; so would the allocator, which would then let
    every deck claim the same uncounted copy. One copy is the floor a NULL
    lot can mean, and counting it is what makes "can I grab it?"
    answerable for those lots at all."""
    oracle_ids = oracle_ids_for_card(conn, name=name, scryfall_id=scryfall_id)
    if oracle_ids:
        placeholders = ",".join("?" for _ in oracle_ids)
        return conn.execute(
            f"""SELECT col.collection_id, COALESCE(col.quantity, 1), col.location
                FROM collection col JOIN cards c ON c.scryfall_id = col.scryfall_id
                WHERE c.oracle_id IN ({placeholders})""",
            tuple(oracle_ids),
        ).fetchall()
    name = (name or "").strip()
    if not name:
        return []
    return conn.execute(
        """SELECT col.collection_id, COALESCE(col.quantity, 1), col.location
           FROM collection col JOIN cards c ON c.scryfall_id = col.scryfall_id
           WHERE c.name = ? COLLATE NOCASE""",
        (name,),
    ).fetchall()


def is_bulk_basic(conn, name=None, scryfall_id=None):
    """True for a basic land you hold as untracked bulk: some lot of it has
    a NULL quantity (schema.sql: "NULL = untracked/bulk quantity (e.g. basic
    lands)"). Such a card has no meaningful copy count, so the allocator
    treats it as unlimited rather than as the single copy a NULL lot counts
    as for everything else — otherwise three decks each wanting a Mountain
    would show as a conflict over one. A basic whose lots are all tracked
    still has a real count and is allocated normally."""
    oracle_ids = oracle_ids_for_card(conn, name=name, scryfall_id=scryfall_id)
    if oracle_ids:
        marks = ",".join("?" for _ in oracle_ids)
        where, params = f"c.oracle_id IN ({marks})", tuple(oracle_ids)
    elif (name or "").strip():
        where, params = "c.name = ? COLLATE NOCASE", (name.strip(),)
    else:
        return False
    return conn.execute(
        f"""SELECT 1 FROM collection col JOIN cards c ON c.scryfall_id = col.scryfall_id
            WHERE c.is_basic_land = 1 AND col.quantity IS NULL AND {where} LIMIT 1""",
        params,
    ).fetchone() is not None


def card_inventory_status(conn, card_name, deck_name=None, scryfall_id=None):
    """Availability snapshot for one card, across every owned printing of
    it — the per-row inventory check shown on the Shortlist tab. A
    collection lot whose Location exactly matches a currently tracked
    deck's name is considered "sleeved in that deck"; anything else
    (blank/NULL, or a plain storage-box label like "Box") counts as
    available.

    Matched on oracle_id (via _lots_for_card), with `scryfall_id` preferred
    over `card_name` when the caller has a linked printing. Name matching
    is only the fallback for a reference with no resolvable oracle_id.

    This answers one row in isolation and therefore CANNOT see that two
    planned changes want the same physical copy — for that, use
    plan_availability(), which allocates across a whole queue.

    Returns a dict:
      {"owned_qty": int,
       "available_lots": [{"collection_id", "quantity", "location"}, ...],
       "in_this_deck_qty": int,
       "in_other_decks": {deck_name: qty, ...}}

    Called directly from pages rather than through a loaders.py
    @st.cache_data wrapper — it drives the Swap Manager's live
    inventory check and must always reflect the current DB state,
    including collection writes made earlier in the same page run.

    `available_lots` excludes anything already sleeved in `deck_name` or
    in another tracked deck, ordered largest-quantity-first (the Swap
    Manager reassigns the first one on execute rather than creating a
    redundant new lot). Returns all-zero/empty for a blank name or a
    name with no collection rows at all."""
    result = {"owned_qty": 0, "available_lots": [], "in_this_deck_qty": 0, "in_other_decks": {}}
    card_name = (card_name or "").strip()
    if not card_name and not scryfall_id:
        return result

    deck_names = deck_name_set(conn)
    rows = _lots_for_card(conn, name=card_name, scryfall_id=scryfall_id)

    for collection_id, quantity, location in rows:
        qty = quantity or 0
        result["owned_qty"] += qty
        loc = (location or "").strip()
        if deck_name and loc == deck_name:
            result["in_this_deck_qty"] += qty
        elif loc in deck_names:
            result["in_other_decks"][loc] = result["in_other_decks"].get(loc, 0) + qty
        else:
            result["available_lots"].append(
                {"collection_id": collection_id, "quantity": qty, "location": location}
            )

    result["available_lots"].sort(key=lambda lot: lot["quantity"], reverse=True)
    return result


# ------------------------------------------------------------------
# The plan allocator (Workbench, Phase 2)
#
# card_inventory_status() above answers one shortlist row against current
# database state. Asked twice about a card you own one of, it says
# "available in storage" twice — neither answer wrong alone, the pair
# impossible. plan_availability() replaces that with a single pass over a
# whole queue: it builds a pool of available lots, walks the changes in a
# fixed order, and lets each row CLAIM copies out of the pool, so the
# second row to want your only Blood Crypt is told which earlier change
# took it.
#
# The same function serves one deck's Shortlist tab and the Workbench's
# all-decks queue — called with a different set of rows, never with
# different logic — so the two views can't disagree about what's free.
# ------------------------------------------------------------------

# The verdicts a shortlist row can get, worst-to-best, so a table can be
# sorted by "what needs attention". `no_add` is the degenerate case of a
# row that removes a card without adding one.
PLAN_VERDICTS = ("not_owned", "wrong_location", "in_other_deck", "claimed_by", "already_here",
                 "available", "no_add")


def _change_sort_key(row):
    """The allocator's walk order: planned time first (falling back to
    created time for an idea, which has no planned_at), then change_id.
    Deterministic and stable, so the same queue allocates the same way on
    every call — otherwise the page would reshuffle who owns a contested
    card between reruns."""
    return (str(row.get("planned_at") or row.get("created_at") or ""), int(row["change_id"]))


def _rows_of(changes):
    """Accept either a DataFrame (what open_changes_dataframe returns) or a
    plain iterable of mappings, so callers and tests needn't agree on one.
    NaN is normalised to None: pandas turns a NULL text column into NaN,
    and `if row["add_scryfall_id"]` is True for NaN."""
    if isinstance(changes, pd.DataFrame):
        records = changes.to_dict("records")
    else:
        records = [dict(r) for r in changes]
    for row in records:
        for key, value in list(row.items()):
            if value is not None and not isinstance(value, str) and pd.isna(value):
                row[key] = None
    return records


def _card_key(conn, row, cache):
    """What two changes must share to be competing for the same physical
    card: its oracle_id. Falls back to the casefolded typed name for a row
    with no resolvable oracle_id, so unlinked rows still contend with each
    other — and, because oracle_ids_for_card() resolves a bare NAME too, an
    unlinked row contends with a linked one for the same card."""
    sid = row.get("add_scryfall_id")
    name = row.get("add_card") or row.get("add_name")
    if not sid and not name:
        return None
    cache_key = (sid, (name or "").casefold())
    if cache_key not in cache:
        oracle_ids = oracle_ids_for_card(conn, name=name, scryfall_id=sid)
        # Several oracle_ids under one name is pathological (distinct cards
        # sharing a name); sorted() just keeps the choice deterministic.
        cache[cache_key] = sorted(oracle_ids)[0] if oracle_ids else f"name:{(name or '').casefold()}"
    return cache[cache_key]


def plan_availability(conn, changes):
    """Allocate your actual copies across a queue of shortlist rows.

    `changes` is a DataFrame or iterable of mappings carrying at least
    change_id, deck_id, deck_name, status, quantity, add_scryfall_id and
    add_card — open_changes_dataframe() returns exactly that. Pass one
    deck's rows or every deck's; the allocation is over whatever you pass,
    which is the point: the Workbench passes everything, so it sees the
    cross-deck collisions a per-deck view cannot.

    Returns {change_id: verdict_dict}, one entry per input row:

      verdict           one of PLAN_VERDICTS
      bulk              True for an untracked-quantity basic land: always
                        available, claims nothing, never in a conflict
      wanted / claimed  copies the row asked for / actually got
      owned_qty         total copies owned, anywhere
      locations         where the claimed copies are sitting
      lot_ids           collection_ids claimed, so a caller applying in
                        this order hits the lots the verdicts named
      claimed_by        earlier changes that took copies of this card:
                        [{change_id, deck_id, deck_name, status}, ...]
      in_other_decks    {deck_name: qty} for copies sleeved elsewhere
      in_this_deck_qty  copies already sleeved in THIS row's deck
      text              one-line summary, for a table cell

    The verdicts, in the order they are decided:

      no_add         the row only removes a card; nothing to source
      already_here   this deck already has a copy sleeved, so adding it is
                     almost certainly not what was meant. The row claims
                     nothing, leaving the copy for someone who needs it
      available      every wanted copy was claimed from storage
      claimed_by     short, and an EARLIER row in this queue took copies —
                     the cross-deck conflict this function exists to catch
      wrong_location the row chose a storage Location (Phase 2) that has no
                     free copy left, nobody took it, but another Location
                     does: change the source, or move the copy
      in_other_deck  short, nobody else claimed it, but you own copies
                     sleeved in other decks: unsleeve one, don't buy one
      not_owned      short and you own none — a purchase (applying would
                     create a starter lot). Phase 3's buy list reads this

    A row wanting 2 copies that gets 1 is reported by the blocker for the
    copy it did NOT get (claimed=1, verdict claimed_by / in_other_deck /
    not_owned), since that shortfall is the actionable half.

    Phase 2: a row with source='storage' claims only from its
    `source_location` (a row without the columns, or with source NULL, is
    Auto, as before). A brewing deck's saved storage choices that it hasn't
    pulled yet (brewing_claims()) are claimed before any row is walked: the
    list has already decided them, whereas a shortlist row is only a plan.

    Phase 3: every open take from another deck (open_takes() — read from the
    database, so a per-deck call sees other decks' takes too) claims that
    deck's sleeved copy before any row is walked, as the brewing claims do.
    The receiving row (source='deck') reads its verdict off that claim:
    `available` ("From Gisa"), `claimed_by` when an earlier take got the
    copy, or why the donor can't give it. An Auto row short of storage whose
    only other copies are all promised this way is `claimed_by` the takers
    rather than `in_other_deck` — unsleeving one would break the promise."""
    rows = sorted(_rows_of(changes), key=_change_sort_key)
    deck_names = deck_name_set(conn)
    key_cache = {}

    # Per card key, built once on first use: the lots free to be claimed
    # (`remaining` decremented as rows claim them), what each deck already
    # holds, the total owned, and who has claimed so far.
    pools = {}

    def pool_for(key, row):
        if key in pools:
            return pools[key]
        lots, deck_holdings, owned = [], {}, 0
        for collection_id, quantity, location in _lots_for_card(
            conn, name=row.get("add_card") or row.get("add_name"),
            scryfall_id=row.get("add_scryfall_id"),
        ):
            qty = quantity or 0
            owned += qty
            loc = (location or "").strip()
            if loc in deck_names:
                deck_holdings[loc] = deck_holdings.get(loc, 0) + qty
            elif qty > 0:
                lots.append({"collection_id": collection_id, "remaining": qty, "location": location})
        # Biggest lot first, matching execute_swap()'s own preference for
        # reassigning an existing lot over creating a redundant one.
        lots.sort(key=lambda lot: (-lot["remaining"], lot["collection_id"]))
        # deck_remaining: each deck's sleeved copies not yet promised to a
        # take; deck_claims: who has been promised one (Phase 3).
        pools[key] = {"lots": lots, "deck_holdings": deck_holdings, "owned": owned, "claims": [],
                      "deck_remaining": dict(deck_holdings), "deck_claims": []}
        return pools[key]

    # A brewing deck's pinned-but-unpulled cards take their copy first.
    for claim in brewing_claims(conn):
        stub = {"add_scryfall_id": claim["scryfall_id"], "add_card": claim["card"]}
        pool = pool_for(_card_key(conn, stub, key_cache), stub)
        owed = claim["quantity"]
        for lot in pool["lots"]:
            if owed <= 0:
                break
            if not same_location(lot["location"], claim["location"]):
                continue
            take = min(lot["remaining"], owed)
            lot["remaining"] -= take
            owed -= take
            if take > 0 and not any(c["deck_id"] == claim["deck_id"] and c["change_id"] is None
                                    for c in pool["claims"]):
                pool["claims"].append({"change_id": None, "deck_id": claim["deck_id"],
                                       "deck_name": claim["deck_name"], "status": "brewing"})

    # Takes from another deck claim the donor's copy, oldest first. Keyed by
    # the RECEIVING row, whose verdict this decides.
    takes = {}
    for take in open_takes(conn):
        stub = {"add_scryfall_id": take["receiver_scryfall_id"] or take["scryfall_id"], "add_card": take["card"]}
        pool = pool_for(_card_key(conn, stub, key_cache), stub)
        donor = take["donor_deck_name"]
        earlier = [c for c in pool["deck_claims"] if c["from"] == donor]
        got = min(pool["deck_remaining"].get(donor, 0), max(1, int(take["quantity"] or 1)))
        pool["deck_remaining"][donor] = pool["deck_remaining"].get(donor, 0) - got
        claimant = {"change_id": take["receiver_change_id"], "deck_id": take["receiver_deck_id"],
                    "deck_name": take["receiver_deck_name"], "status": take["receiver_status"],
                    "from": donor}
        if got:
            pool["deck_claims"].append(claimant)
        takes[take["receiver_change_id"]] = {"claimed": got, "earlier": earlier, "donor": donor,
                                             "held": pool["deck_holdings"].get(donor, 0)}

    out = {}
    for row in rows:
        change_id = int(row["change_id"])
        wanted = max(1, int(row.get("quantity") or 1))
        deck_name = row.get("deck_name")
        # A chosen storage Location (Phase 2); anything else is Auto.
        pinned = ((row.get("source_location") or "").strip()
                  if row.get("source") == "storage" else "")
        verdict = {
            "verdict": "no_add", "wanted": wanted, "claimed": 0, "owned_qty": 0,
            "locations": [], "lot_ids": [], "claimed_by": [], "in_other_decks": {},
            "in_this_deck_qty": 0, "text": "Removal only — nothing to source",
        }
        key = _card_key(conn, row, key_cache)
        if key is None:
            out[change_id] = verdict
            continue

        pool = pool_for(key, row)
        verdict["owned_qty"] = pool["owned"]
        in_this_deck = pool["deck_holdings"].get(deck_name, 0) if deck_name else 0
        verdict["in_this_deck_qty"] = in_this_deck
        verdict["in_other_decks"] = {
            d: n for d, n in sorted(pool["deck_holdings"].items()) if d != deck_name
        }

        if in_this_deck:
            verdict["verdict"] = "already_here"
            verdict["text"] = f"Already sleeved in {deck_name} (x{in_this_deck})"
            out[change_id] = verdict
            continue

        if is_bulk_basic(conn, name=row.get("add_card") or row.get("add_name"),
                         scryfall_id=row.get("add_scryfall_id")):
            verdict["verdict"] = "available"
            verdict["bulk"] = True
            verdict["claimed"] = wanted
            verdict["text"] = "Bulk basic land — untracked quantity"
            out[change_id] = verdict
            continue

        if row.get("source") == "deck":
            # A take from another deck (Phase 3): settled by the claim made
            # above, never from storage.
            donor = (row.get("source_location") or "").strip()
            take = takes.get(change_id) or {"claimed": 0, "earlier": [], "donor": donor,
                                            "held": pool["deck_holdings"].get(donor, 0)}
            verdict["claimed"] = min(wanted, take["claimed"])
            got = f" (got {verdict['claimed']} of {wanted})" if verdict["claimed"] else ""
            if verdict["claimed"] >= wanted:
                verdict["verdict"] = "available"
                verdict["locations"] = [donor]
                verdict["text"] = f"From {donor} (your pick)"
            elif take["earlier"]:
                verdict["verdict"] = "claimed_by"
                verdict["claimed_by"] = [{k: c[k] for k in ("change_id", "deck_id", "deck_name", "status")}
                                         for c in take["earlier"]]
                who = ", ".join(f"{c['deck_name']} (#{c['change_id']})" for c in take["earlier"])
                verdict["text"] = f"{donor}'s copy is promised to {who} first{got}"
            elif any(lot["remaining"] > 0 for lot in pool["lots"]):
                verdict["verdict"] = "wrong_location"
                free_at = sorted({(lot["location"] or "").strip() or "location unknown"
                                  for lot in pool["lots"] if lot["remaining"] > 0})
                verdict["text"] = f"None sleeved in {donor}{got} — free in {', '.join(free_at)}"
            elif verdict["in_other_decks"]:
                verdict["verdict"] = "in_other_deck"
                verdict["text"] = f"None sleeved in {donor}{got}"
            else:
                verdict["verdict"] = "not_owned"
                verdict["text"] = f"None sleeved in {donor}, none owned elsewhere — buy{got}"
            out[change_id] = verdict
            continue

        remaining = wanted
        for lot in pool["lots"]:
            if remaining <= 0:
                break
            if pinned and not same_location(lot["location"], pinned):
                continue
            take = min(lot["remaining"], remaining)
            if take <= 0:
                continue
            lot["remaining"] -= take
            remaining -= take
            verdict["claimed"] += take
            verdict["lot_ids"].append(lot["collection_id"])
            verdict["locations"].append((lot["location"] or "").strip() or "location unknown")

        if verdict["claimed"]:
            pool["claims"].append({
                "change_id": change_id, "deck_id": row.get("deck_id"),
                "deck_name": deck_name, "status": row.get("status"),
            })

        if remaining <= 0:
            verdict["verdict"] = "available"
            verdict["text"] = "In storage: " + ", ".join(sorted(set(verdict["locations"])))
            if pinned:
                verdict["text"] += " (your pick)"
        else:
            # Earlier claimants only — this row's own claim is excluded.
            earlier = [c for c in pool["claims"] if c["change_id"] != change_id]
            verdict["claimed_by"] = earlier
            got = f" (got {verdict['claimed']} of {wanted})" if verdict["claimed"] else ""
            if earlier:
                verdict["verdict"] = "claimed_by"
                who = ", ".join(
                    f"{c['deck_name']} (#{c['change_id']})" if c["change_id"] is not None
                    else f"{c['deck_name']} (brewing)" for c in earlier)
                verdict["text"] = f"Claimed first by {who}{got}"
            elif pinned and any(lot["remaining"] > 0 for lot in pool["lots"]):
                # Free copies exist, just not in the Location this row named.
                elsewhere = sorted({(lot["location"] or "").strip() or "location unknown"
                                    for lot in pool["lots"] if lot["remaining"] > 0})
                verdict["verdict"] = "wrong_location"
                verdict["text"] = f"None free in {pinned}{got} — free in {', '.join(elsewhere)}"
            elif verdict["in_other_decks"] and not any(
                    pool["deck_remaining"].get(d, 0) > 0 for d in verdict["in_other_decks"]) \
                    and pool["deck_claims"]:
                # Every copy in another deck is already promised to a take.
                takers = [{k: c[k] for k in ("change_id", "deck_id", "deck_name", "status")}
                          for c in pool["deck_claims"] if c["deck_name"] != deck_name]
                verdict["verdict"] = "claimed_by"
                verdict["claimed_by"] = takers
                who = ", ".join(f"{c['deck_name']} (from {c['from']})" for c in pool["deck_claims"])
                verdict["text"] = f"Only in other decks, promised to {who}{got}"
            elif verdict["in_other_decks"]:
                verdict["verdict"] = "in_other_deck"
                who = ", ".join(f"{d} (x{n})" for d, n in verdict["in_other_decks"].items())
                verdict["text"] = f"Only in: {who}{got}"
            else:
                verdict["verdict"] = "not_owned"
                verdict["text"] = "Not owned — buy" + got
        out[change_id] = verdict
    return out


def plan_conflicts(conn, changes, availability=None):
    """The rows of `changes` that cannot all succeed, grouped by card.

    A conflict is TWO OR MORE open changes wanting one card and, between
    them, wanting more copies than are free in storage. One row wanting a
    card you don't own is not a conflict — it's a purchase (Phase 3's buy
    list); one row wanting a card sleeved in another deck isn't either —
    it's an unsleeve. Only genuine competition appears here, because that's
    what needs a decision rather than an action.

    `availability` reuses an existing plan_availability() result instead of
    re-allocating. Returns a list of dicts, biggest shortfall first:

      {card, owned_qty, available_qty, promised_qty, wanted_qty, shortfall,
       wanters: [{change_id, deck_id, deck_name, status, quantity,
                  verdict}, ...]}

    `available_qty` is copies in storage (sleeved nowhere), which is what
    the competing rows are actually competing over. `promised_qty` (Phase
    3) is the other-deck copies the group's takes were promised: supply
    too, so a take plus a row served from storage isn't a conflict, but a
    take of a deck's only copy plus a row with nowhere else to look is."""
    rows = sorted(_rows_of(changes), key=_change_sort_key)
    if availability is None:
        availability = plan_availability(conn, rows)
    deck_names = deck_name_set(conn)
    key_cache, groups = {}, {}

    for row in rows:
        change_id = int(row["change_id"])
        verdict = availability.get(change_id, {})
        # An already_here row isn't competing — it claims nothing.
        if verdict.get("verdict") in ("no_add", "already_here") or verdict.get("bulk"):
            continue
        key = _card_key(conn, row, key_cache)
        if key is None:
            continue
        quantity = max(1, int(row.get("quantity") or 1))
        group = groups.setdefault(key, {
            "card": row.get("add_card") or row.get("add_name") or "(unnamed)",
            "owned_qty": verdict.get("owned_qty", 0),
            "available_qty": None,
            "promised_qty": 0,
            "wanted_qty": 0,
            "wanters": [],
        })
        group["wanted_qty"] += quantity
        if row.get("source") == "deck":
            # A take (Phase 3) is supplied by the donor deck's copy it was
            # promised, not by storage — count that copy as supply too.
            group["promised_qty"] += verdict.get("claimed", 0)
        group["wanters"].append({
            "change_id": change_id, "deck_id": row.get("deck_id"),
            "deck_name": row.get("deck_name"), "status": row.get("status"),
            "quantity": quantity, "verdict": verdict.get("verdict"),
        })
        if group["available_qty"] is None:
            free = 0
            for _cid, quantity_in_lot, location in _lots_for_card(
                conn, name=row.get("add_card") or row.get("add_name"),
                scryfall_id=row.get("add_scryfall_id"),
            ):
                if (location or "").strip() not in deck_names:
                    free += quantity_in_lot or 0
            group["available_qty"] = free

    conflicts = [
        dict(g, shortfall=g["wanted_qty"] - (g["available_qty"] or 0) - g["promised_qty"])
        for g in groups.values()
        if len(g["wanters"]) > 1 and g["wanted_qty"] > (g["available_qty"] or 0) + g["promised_qty"]
    ]
    conflicts.sort(key=lambda g: (-g["shortfall"], g["card"].casefold()))
    return conflicts


def unlocated_lot_summary(conn):
    """Collection lots with no Location at all. "Can I grab it?" has no
    answer for these, which is what Phase 4's reconciliation is for;
    surfaced in the Workbench header so the number doesn't stay invisible
    until then. Returns {lots, copies, value}."""
    row = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(COALESCE(col.quantity, 1)), 0),
                  COALESCE(SUM(COALESCE(col.quantity, 1) * COALESCE(c.current_price_usd, 0)), 0)
           FROM collection col LEFT JOIN cards c ON c.scryfall_id = col.scryfall_id
           WHERE col.location IS NULL OR TRIM(col.location) = ''"""
    ).fetchone()
    return {"lots": row[0] or 0, "copies": int(row[1] or 0), "value": float(row[2] or 0.0)}


# ------------------------------------------------------------------
# Reconciliation (Workbench rework, Phase 4)
#
# A deck's LIST (deck_cards) and its PHYSICAL contents (collection lots
# whose Location is the deck's name) are maintained separately and drift:
# a swap leaves the cut card's lot behind, a deck built from a Moxfield
# import has a list and no sleeved lots at all. These functions measure
# the gap in both directions. Matching is by oracle_id, like every other
# ownership question here, so a different printing of the same card
# sleeved in the deck counts. Basics are excluded throughout — nobody
# sleeves a Swamp lot per deck.
# ------------------------------------------------------------------

def unlocated_lots(conn):
    """Every collection lot with no Location, most valuable first so the
    expensive unknowns get boxed first. Rows are dicts: collection_id, card,
    set_code, quantity (COALESCEd to 1), foil, price (unit), value
    (quantity x price), source, date_acquired."""
    rows = conn.execute(
        """SELECT col.collection_id, c.name, c.set_code, COALESCE(col.quantity, 1),
                  col.foil, COALESCE(c.current_price_usd, 0), col.source, col.date_acquired
           FROM collection col LEFT JOIN cards c ON c.scryfall_id = col.scryfall_id
           WHERE col.location IS NULL OR TRIM(col.location) = ''"""
    ).fetchall()
    out = [{
        "collection_id": r[0], "card": r[1] or "(unknown card)", "set_code": r[2] or "",
        "quantity": int(r[3]), "foil": bool(r[4]), "price": float(r[5]),
        "value": float(r[5]) * int(r[3]), "source": r[6] or "", "date_acquired": r[7] or "",
    } for r in rows]
    out.sort(key=lambda r: (-r["value"], r["card"].casefold()))
    return out


def deck_reconciliation(conn, deck_id=None):
    """List-vs-physical diff for every deck (or just `deck_id`), non-basics
    only. Returns one dict per deck, in deck-name order:

      {"deck_id", "deck_name",
       "to_pull":     [{"oracle_id", "card", "needed", "located", "short",
                        "scryfall_id", "candidates"}, ...],
       "to_put_away": [{"oracle_id", "card", "needed", "located", "surplus",
                        "lots"}, ...],
       "build": {"needed", "located", "pct"}}

    *to_pull* — mainboard copies with no lot located to this deck
    (`short` = needed - located). `candidates` are the card's lots that
    could be sleeved here, storage/unlocated first, then lots currently in
    another deck, each {collection_id, quantity, location, in_deck}; `scryfall_id`
    is the mainboard printing, for display.
    *to_put_away* — lots located to this deck for a card the mainboard
    doesn't run (or runs fewer of): the leftovers of past swaps.
    `lots` are the deck's located lots of that card.
    *build* — `located` counts at most `needed` per card, so a surplus lot
    can't hide a shortfall elsewhere; `pct` is None for a deck with no
    non-basic mainboard cards (nothing to be built)."""
    all_decks = {d[0]: d[1] for d in conn.execute("SELECT deck_id, name FROM decks")}
    deck_names = all_decks if deck_id is None else {k: v for k, v in all_decks.items() if k == deck_id}

    # A card with no oracle_id yet keys on its lowercased name instead, the
    # same fallback _lots_for_card() uses.
    key = "COALESCE(c.oracle_id, 'name:' || LOWER(c.name))"

    needed = {}   # (deck_id, key) -> [qty, name, scryfall_id]
    for did, k, name, sid, qty in conn.execute(
        f"""SELECT dc.deck_id, {key}, c.name, dc.scryfall_id, dc.quantity
            FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
            WHERE COALESCE(c.is_basic_land, 0) = 0"""
    ):
        if did not in deck_names:
            continue
        slot = needed.setdefault((did, k), [0, name, sid])
        slot[0] += qty or 0

    lots = {}     # key -> [{collection_id, quantity, location, card}]
    for cid, k, name, qty, loc in conn.execute(
        f"""SELECT col.collection_id, {key}, c.name, COALESCE(col.quantity, 1), col.location
            FROM collection col JOIN cards c ON c.scryfall_id = col.scryfall_id
            WHERE COALESCE(c.is_basic_land, 0) = 0"""
    ):
        lots.setdefault(k, []).append(
            {"collection_id": cid, "quantity": int(qty), "location": (loc or "").strip(), "card": name}
        )

    # Every deck, not just the requested one: a lot sleeved in ANOTHER deck must still read as such.
    by_name = {v: k for k, v in all_decks.items()}
    result = []
    for did, dname in sorted(deck_names.items(), key=lambda kv: kv[1].casefold()):
        deck_keys = {k for (d, k) in needed if d == did}
        # Keys with a lot located here but possibly not in the mainboard.
        located_keys = {k for k, ls in lots.items() if any(l["location"] == dname for l in ls)}
        to_pull, to_put_away = [], []
        total_needed = total_located = 0
        for k in sorted(deck_keys | located_keys):
            need, name, sid = needed.get((did, k), [0, None, None])
            here = [l for l in lots.get(k, []) if l["location"] == dname]
            located = sum(l["quantity"] for l in here)
            total_needed += need
            total_located += min(need, located)
            card = name or (here[0]["card"] if here else k)
            if need > located:
                candidates = [
                    {"collection_id": l["collection_id"], "quantity": l["quantity"],
                     "location": l["location"], "in_deck": l["location"] in by_name}
                    for l in lots.get(k, []) if l["location"] != dname
                ]
                # Free lots (storage / unlocated) before ones raided from another deck.
                candidates.sort(key=lambda l: (l["in_deck"], -l["quantity"]))
                to_pull.append({
                    "oracle_id": k, "card": card, "needed": need, "located": located,
                    "short": need - located, "scryfall_id": sid, "candidates": candidates,
                })
            elif located > need:
                to_put_away.append({
                    "oracle_id": k, "card": card, "needed": need, "located": located,
                    "surplus": located - need, "lots": here,
                })
        to_pull.sort(key=lambda r: r["card"].casefold())
        to_put_away.sort(key=lambda r: r["card"].casefold())
        result.append({
            "deck_id": did, "deck_name": dname,
            "to_pull": to_pull, "to_put_away": to_put_away,
            "build": {
                "needed": total_needed, "located": total_located,
                "pct": (100.0 * total_located / total_needed) if total_needed else None,
            },
        })
    return result


# ------------------------------------------------------------------
# Deck lifecycle: brewing + dismantling (Phase 6)
#
# deck_reconciliation() above already answers "what is missing from this
# deck", which is most of a brew. What it does not answer is *where each
# missing copy would come from* — and that is the whole question when the
# deck doesn't physically exist yet. deck_sourcing_plan() allocates every
# needed copy to exactly one source, so the buckets are mutually exclusive
# and sum to the deck's non-basic count; deck_dismantle_plan() runs the
# same machinery backwards, splitting a deck's sleeved lots into the ones
# a target deck can absorb card-to-card and the ones going back in the box.
#
# Both are read-only. Staging either result as planned deck_changes rows is
# writes.stage_dismantle()'s job.
# ------------------------------------------------------------------

# The sources a needed copy can come from, in the order they are claimed.
# Order matters twice: it is the priority a copy is allocated with (a card
# you have in the box is sourced from the box, never from another deck),
# and it is the order the Build Plan lists the buckets in.
SOURCING_BUCKETS = ("sleeved", "box", "unlocated", "other_deck", "not_owned")

_BUCKET_LABELS = {
    "sleeved": "Already sleeved here",
    "box": "From the box",
    "unlocated": "Owned, location unknown",
    "other_deck": "From another deck",
    "not_owned": "Not owned",
}


def bucket_label(bucket):
    """Human label for a SOURCING_BUCKETS key (shared by the Build Plan
    panel and the printable pull sheet so the two can't drift)."""
    return _BUCKET_LABELS.get(bucket, bucket)


def deck_sourcing_plan(conn, deck_id):
    """Where every non-basic copy this deck's mainboard needs would come
    from — the Build Plan behind brewing a deck that isn't sleeved yet.

    Returns None for an unknown deck_id, else:

      {"deck_id", "deck_name", "build_state",
       "needed": int, "located": int, "pct": float_or_None,
       "buckets": {bucket: [card_row, ...]},   # keys are SOURCING_BUCKETS
       "counts":  {bucket: copies},            # sums to `needed`
       "donors":  [donor_row, ...]}

    where a *card_row* is

      {"oracle_id", "card", "scryfall_id", "copies", "needed",
       "locations": [str, ...],
       "lots": [{collection_id, quantity, location, deck}, ...]}

    `copies` is how many copies this bucket accounts for, not how many the
    deck runs (`needed`) — one card appears in two buckets when it needs
    two copies that sit in different places — and `lots` lists only the
    lots this bucket's copies were claimed from, with `quantity` cut down
    to the number actually claimed from that lot.

    A *donor_row* is

      {"deck_id", "deck_name", "copies", "cards": [card_row, ...],
       "shortfall": int}

    `shortfall` is how many cards the donor deck would newly be missing if
    every copy listed were pulled out of it — the donor-impact warning that
    is the point of this whole view. It counts only copies that donor deck's
    own mainboard actually runs: raiding a leftover lot sitting in a deck
    that no longer plays the card costs that deck nothing.

    **Every needed copy lands in exactly one bucket**, allocated in
    SOURCING_BUCKETS order. That is what makes the buckets safe to sum, and
    it is the bug the design exists to prevent: a card with one lot in the
    box and one sleeved in another deck would otherwise read as both "in
    the box" and "in deck X", double-counting a single requirement. A copy
    is claimed from storage first, and only the genuine shortfall is
    attributed to a donor.

    Basics are excluded throughout, the same `is_basic_land` filter
    deck_reconciliation() uses — you don't source Swamps out of another
    deck. A quantity-NULL lot counts as one copy, the floor
    _lots_for_card() documents.

    Not cached: it reads collection state the page may have just written
    (sleeving one card changes the answer), the same reason
    card_inventory_status() bypasses loaders.py.
    """
    row = conn.execute(
        "SELECT deck_id, name, build_state FROM decks WHERE deck_id=?", (deck_id,)
    ).fetchone()
    if row is None:
        return None
    deck_name = row[1]

    # Same oracle-or-lowercased-name key deck_reconciliation() and
    # _lots_for_card() use, so a card whose oracle_id was never synced
    # still groups its printings together instead of splitting in two.
    key = "COALESCE(c.oracle_id, 'name:' || LOWER(c.name))"
    deck_ids_by_name = {r[0]: r[1] for r in conn.execute("SELECT name, deck_id FROM decks")}

    needed = {}   # key -> [copies, card_name, scryfall_id]
    for k, name, sid, qty in conn.execute(
        f"""SELECT {key}, c.name, dc.scryfall_id, dc.quantity
            FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
            WHERE dc.deck_id = ? AND COALESCE(c.is_basic_land, 0) = 0""",
        (deck_id,),
    ):
        slot = needed.setdefault(k, [0, name, sid])
        slot[0] += qty or 0

    # Every lot of every card this deck wants, so each copy can be traced
    # to the box, to nowhere in particular, or to the deck holding it.
    lots = {}
    if needed:
        marks = ",".join("?" for _ in needed)
        for cid, k, qty, loc in conn.execute(
            f"""SELECT col.collection_id, {key}, COALESCE(col.quantity, 1), col.location
                FROM collection col JOIN cards c ON c.scryfall_id = col.scryfall_id
                WHERE {key} IN ({marks})""",
            tuple(needed),
        ):
            lots.setdefault(k, []).append(
                {"collection_id": cid, "quantity": int(qty), "location": (loc or "").strip()}
            )

    # What each OTHER deck needs of these cards, for the donor-impact maths.
    donor_need = {}   # (deck_id, key) -> copies
    if needed:
        marks = ",".join("?" for _ in needed)
        for did, k, qty in conn.execute(
            f"""SELECT dc.deck_id, {key}, dc.quantity
                FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
                WHERE {key} IN ({marks}) AND dc.deck_id != ?
                  AND COALESCE(c.is_basic_land, 0) = 0""",
            (*needed, deck_id),
        ):
            donor_need[(did, k)] = donor_need.get((did, k), 0) + (qty or 0)

    buckets = {b: [] for b in SOURCING_BUCKETS}
    counts = {b: 0 for b in SOURCING_BUCKETS}
    donors = {}       # deck_name -> donor_row
    total_needed = total_located = 0

    for k, (need, card, sid) in sorted(needed.items(), key=lambda kv: kv[1][1].casefold()):
        if need <= 0:
            continue
        total_needed += need
        card_lots = lots.get(k, [])
        here = [l for l in card_lots if l["location"] == deck_name]
        free = [l for l in card_lots if l["location"] not in deck_ids_by_name]
        elsewhere = [
            l for l in card_lots
            if l["location"] in deck_ids_by_name and l["location"] != deck_name
        ]

        def row_for(bucket, copies, claimed):
            """Record one bucket's share of this card, with the lots the
            copies were claimed from."""
            counts[bucket] += copies
            entry = {
                "oracle_id": k, "card": card, "scryfall_id": sid,
                "copies": copies, "needed": need, "lots": claimed,
                "locations": sorted({l["location"] or "location unknown" for l in claimed}),
            }
            buckets[bucket].append(entry)
            return entry

        # 1. Already sleeved here — capped at `need` so a surplus lot can't
        #    mask a shortfall elsewhere (deck_reconciliation()'s rule, kept
        #    identical so the two views report the same build %).
        sleeved = min(need, sum(l["quantity"] for l in here))
        if sleeved:
            row_for("sleeved", sleeved, here)
        total_located += sleeved
        remaining = need - sleeved

        # 2/3. Free lots: a named storage location is "in the box", a blank
        #      one is "owned, location unknown" (→ Reconcile, Phase 4).
        #      Named first — a copy you can walk to beats one you must hunt.
        for bucket, pool in (
            ("box", [l for l in free if l["location"]]),
            ("unlocated", [l for l in free if not l["location"]]),
        ):
            if remaining <= 0:
                break
            claimed, took = [], 0
            for lot in pool:
                if took >= remaining:
                    break
                take = min(lot["quantity"], remaining - took)
                took += take
                claimed.append({**lot, "quantity": take, "deck": None})
            if took:
                row_for(bucket, took, claimed)
                remaining -= took

        # 4. Another deck's copy — the contested case, attributed per donor.
        if remaining > 0 and elsewhere:
            claimed, took = [], 0
            for lot in elsewhere:
                if took >= remaining:
                    break
                take = min(lot["quantity"], remaining - took)
                took += take
                claimed.append({**lot, "quantity": take, "deck": lot["location"]})
            entry = row_for("other_deck", took, claimed)
            remaining -= took
            for lot in claimed:
                dname = lot["location"]
                did = deck_ids_by_name.get(dname)
                d = donors.setdefault(
                    dname,
                    {"deck_id": did, "deck_name": dname, "copies": 0, "cards": [], "shortfall": 0},
                )
                d["copies"] += lot["quantity"]
                d["cards"].append(
                    {**entry, "copies": lot["quantity"], "lots": [lot], "locations": [dname]}
                )
                # How much of THIS donor's own satisfied need we would break.
                # A deck already short of the card loses nothing further, and
                # a deck that doesn't run it at all loses nothing at all.
                d_need = donor_need.get((did, k), 0)
                d_has = sum(l["quantity"] for l in card_lots if l["location"] == dname)
                before = max(0, d_need - min(d_need, d_has))
                after = max(0, d_need - max(0, d_has - lot["quantity"]))
                d["shortfall"] += max(0, after - before)

        # 5. Whatever is left you simply don't own → the buy list.
        if remaining > 0:
            row_for("not_owned", remaining, [])

    return {
        "deck_id": deck_id, "deck_name": deck_name, "build_state": row[2],
        "needed": total_needed, "located": total_located,
        "pct": (100.0 * total_located / total_needed) if total_needed else None,
        "buckets": buckets, "counts": counts,
        # Biggest raid first — that is the decision that needs making.
        "donors": sorted(donors.values(), key=lambda d: (-d["copies"], d["deck_name"].casefold())),
    }


def deck_dismantle_plan(conn, deck_id, target_deck_id=None):
    """Taking a deck apart, optionally feeding another one: which of its
    cards the target can absorb card-to-card, which go back to storage, and
    what the target would still be missing afterwards.

    Returns None for an unknown deck_id, else:

      {"deck_id", "deck_name", "build_state",
       "sleeved_lots", "sleeved_copies", "value",
       "target_deck_id", "target_deck_name",
       "direct_transfer": [row, ...],
       "to_box":          [row, ...],
       "strays":          [row, ...],
       "target_needs":    [{"oracle_id", "card", "scryfall_id", "short"}, ...]}

    where a *row* is

      {"oracle_id", "card", "card_type", "copies",
       "scryfall_id",          # the sleeved lot's printing, for display
       "source_scryfall_id",   # this deck's mainboard printing, or None
       "target_scryfall_id",   # the target's mainboard printing, or None
       "lots": [{collection_id, quantity, location}, ...]}

    *direct_transfer* — cards on this deck's list that the target's list
    also runs and is not already holding. These move source → target
    without a stop in the box, which is the one piece of information a
    plain delete_deck() throws away.
    *to_box* — the rest of this deck's list that is physically sleeved
    here. With no target, every sleeved mainboard card lands here.
    *strays* — lots sleeved in this deck whose card is NOT on its list:
    the leftovers of past swaps. They are not list changes, so they are
    reported separately and moved by writes.move_dismantle_strays() rather
    than staged as deck_changes rows (Phase 4's Reconcile "put away" is the
    same concern and the same fix).
    *target_needs* — what the target is still short of after the transfer,
    so "feed Gisa with Vilis" ends with an honest list rather than the
    impression the target is now complete.

    **Three printings per row, and they are not interchangeable.** The lot
    sleeved in a deck may be a different printing from the one on either
    deck's list, so a removal has to name the source's own mainboard
    printing and an add has to name the target's — using the lot's printing
    for both is how a dismantle would silently fail to cut anything
    ("no longer in this deck's mainboard") or add a printing the target
    never chose.

    Only copies the target can actually use transfer: a target running one
    copy and already holding it absorbs nothing, so the card goes to the
    box. The target's needs exclude basics (you don't transfer Swamps) but a
    basic sleeved in the source still has to go somewhere, so it appears in
    to_box or strays like anything else.

    Read-only. writes.stage_dismantle() turns the result into planned
    deck_changes rows.
    """
    row = conn.execute(
        "SELECT deck_id, name, build_state FROM decks WHERE deck_id=?", (deck_id,)
    ).fetchone()
    if row is None:
        return None
    deck_name = row[1]

    target = None
    if target_deck_id is not None and target_deck_id != deck_id:
        target = conn.execute(
            "SELECT deck_id, name FROM decks WHERE deck_id=?", (target_deck_id,)
        ).fetchone()
    target_name = target[1] if target else None

    key = "COALESCE(c.oracle_id, 'name:' || LOWER(c.name))"

    def mainboard(did, nonbasic_only=False):
        """{key: [copies, name, scryfall_id]} for one deck's mainboard."""
        extra = "AND COALESCE(c.is_basic_land, 0) = 0" if nonbasic_only else ""
        out = {}
        for k, name, sid, qty in conn.execute(
            f"""SELECT {key}, c.name, dc.scryfall_id, dc.quantity
                FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
                WHERE dc.deck_id = ? {extra}""",
            (did,),
        ):
            slot = out.setdefault(k, [0, name, sid])
            slot[0] += qty or 0
        return out

    source_list = mainboard(deck_id)

    # Everything physically sleeved in the deck being taken apart. Basics
    # included: a Swamp in these sleeves still has to go somewhere.
    sleeved = {}
    total_copies = 0
    value = 0.0
    for cid, k, name, sid, qty, type_line, price in conn.execute(
        f"""SELECT col.collection_id, {key}, c.name, c.scryfall_id,
                   COALESCE(col.quantity, 1), c.type_line, c.current_price_usd
            FROM collection col JOIN cards c ON c.scryfall_id = col.scryfall_id
            WHERE col.location = ?
            ORDER BY col.collection_id""",
        (deck_name,),
    ):
        qty = int(qty)
        slot = sleeved.setdefault(
            k,
            {"oracle_id": k, "card": name, "scryfall_id": sid,
             "card_type": (type_line or "").split("—")[0].strip(),
             "copies": 0, "lots": []},
        )
        slot["copies"] += qty
        slot["lots"].append({"collection_id": cid, "quantity": qty, "location": deck_name})
        total_copies += qty
        value += (price or 0) * qty

    # What the target still needs: its non-basic list minus what it holds.
    target_need = {}
    target_list = {}
    if target:
        target_list = mainboard(target[0], nonbasic_only=True)
        held = {}
        for k, qty in conn.execute(
            f"""SELECT {key}, COALESCE(col.quantity, 1)
                FROM collection col JOIN cards c ON c.scryfall_id = col.scryfall_id
                WHERE col.location = ?""",
            (target_name,),
        ):
            held[k] = held.get(k, 0) + int(qty)
        for k, (want, name, sid) in target_list.items():
            target_need[k] = [max(0, want - held.get(k, 0)), name, sid]

    direct, to_box, strays = [], [], []
    for k, entry in sorted(sleeved.items(), key=lambda kv: kv[1]["card"].casefold()):
        entry = {
            **entry,
            "source_scryfall_id": source_list.get(k, [0, None, None])[2],
            "target_scryfall_id": target_list.get(k, [0, None, None])[2],
        }
        if k not in source_list:
            # Sleeved here but not on this deck's list — a leftover, not a
            # list change. See the docstring on *strays*.
            strays.append(entry)
            continue

        want = target_need.get(k, [0])[0]
        move = min(want, entry["copies"])
        if move:
            claimed, took = [], 0
            for lot in entry["lots"]:
                if took >= move:
                    break
                take = min(lot["quantity"], move - took)
                took += take
                claimed.append({**lot, "quantity": take})
            direct.append({**entry, "copies": move, "lots": claimed})
            target_need[k][0] = want - move
        left = entry["copies"] - move
        if left:
            # The lots (or lot remainders) the target could not use.
            remainder, skip = [], move
            for lot in entry["lots"]:
                if skip >= lot["quantity"]:
                    skip -= lot["quantity"]
                    continue
                remainder.append({**lot, "quantity": lot["quantity"] - skip})
                skip = 0
            to_box.append({**entry, "copies": left, "lots": remainder})

    target_needs = sorted(
        (
            {"oracle_id": k, "card": name, "scryfall_id": sid, "short": short}
            for k, (short, name, sid) in target_need.items()
            if short > 0
        ),
        key=lambda r: r["card"].casefold(),
    )

    return {
        "deck_id": deck_id, "deck_name": deck_name, "build_state": row[2],
        "sleeved_lots": sum(len(e["lots"]) for e in sleeved.values()),
        "sleeved_copies": total_copies, "value": value,
        "target_deck_id": target[0] if target else None,
        "target_deck_name": target_name,
        "direct_transfer": direct, "to_box": to_box, "strays": strays,
        "target_needs": target_needs,
    }

# ------------------------------------------------------------------
# Contention, buy list, Card Lookup (Workbench rework, Phase 3)
#
# Demand vs. supply per oracle card — the question plan_availability()
# deliberately doesn't answer, because it only sees the open shortlist
# queue. Contention also counts cards already IN a mainboard: War Room run
# in 4 decks with 3 copies owned is real demand even though nobody's
# shortlist mentions it.
# ------------------------------------------------------------------

def _resolve_change_oracle(conn, add_scryfall_id, add_name, add_oracle_id):
    """The oracle_id + representative name + basic-land flag for a
    deck_changes add side, resolving an unlinked typed name the same way
    the allocator does (oracle_ids_for_card), so a backfilled swap with no
    add_scryfall_id still contends for the right card. Returns
    (oracle_id_or_None, name_or_None, is_basic_land)."""
    if add_oracle_id:
        row = conn.execute(
            "SELECT is_basic_land FROM cards WHERE oracle_id = ? LIMIT 1", (add_oracle_id,)
        ).fetchone()
        return add_oracle_id, None, bool(row[0]) if row else False
    if add_name:
        ids = oracle_ids_for_card(conn, name=add_name)
        if ids:
            oracle_id = sorted(ids)[0]
            row = conn.execute(
                "SELECT name, is_basic_land FROM cards WHERE oracle_id = ? LIMIT 1", (oracle_id,)
            ).fetchone()
            return oracle_id, (row[0] if row else None), bool(row[1]) if row else False
    return None, None, False


def contention_table(conn):
    """Demand vs. supply per non-basic oracle card, shortfall rows only
    (demand > owned), biggest shortfall first:

        demand    = (# decks with the card in deck_cards)
                  + (# decks with an open idea/planned add for it)
        owned     = SUM(COALESCE(collection.quantity, 1)) across every
                    owned printing of the oracle card
        shortfall = demand - owned

    Matched on oracle_id throughout (mainboard rows directly via
    deck_cards -> cards.oracle_id; shortlist rows via _resolve_change_oracle,
    which falls back to a name match for a backfilled swap with no linked
    printing) — a name match alone would double-count a modal double-faced
    card referenced by either face as two different cards.

    A function rather than a SQL view (per docs/archive/IMPLEMENTATION_PLAN.md): the
    deck_changes half of demand needs status filtering ('idea', 'planned')
    that Phase 5 will extend as history grows, and a view would freeze
    today's definition.

    Returns a list of dicts:
        {oracle_id, card, owned_qty, demand, shortfall,
         mainboard_decks: [name, ...], shortlist_decks: [name, ...]}"""
    deck_id_to_name = {r[0]: r[1] for r in conn.execute("SELECT deck_id, name FROM decks").fetchall()}

    mainboard = {}
    for oracle_id, name, deck_id in conn.execute(
        """SELECT c.oracle_id, c.name, dc.deck_id
           FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
           WHERE c.oracle_id IS NOT NULL AND c.is_basic_land = 0"""
    ).fetchall():
        entry = mainboard.setdefault(oracle_id, {"name": name, "decks": set()})
        entry["decks"].add(deck_id)

    shortlist = {}
    for deck_id, add_sid, add_name, add_oracle_id in conn.execute(
        """SELECT dc.deck_id, dc.add_scryfall_id, dc.add_name, ac.oracle_id
           FROM deck_changes dc LEFT JOIN cards ac ON ac.scryfall_id = dc.add_scryfall_id
           WHERE dc.status IN ('idea', 'planned')
             AND (dc.add_scryfall_id IS NOT NULL OR dc.add_name IS NOT NULL)"""
    ).fetchall():
        oracle_id, name, is_basic = _resolve_change_oracle(conn, add_sid, add_name, add_oracle_id)
        if not oracle_id or is_basic:
            continue
        entry = shortlist.setdefault(oracle_id, {"name": name, "decks": set()})
        entry["decks"].add(deck_id)

    owned = {
        r[0]: r[1] or 0
        for r in conn.execute(
            """SELECT c.oracle_id, SUM(COALESCE(col.quantity, 1))
               FROM collection col JOIN cards c ON c.scryfall_id = col.scryfall_id
               WHERE c.oracle_id IS NOT NULL AND c.is_basic_land = 0
               GROUP BY c.oracle_id"""
        ).fetchall()
    }

    out = []
    for oracle_id in set(mainboard) | set(shortlist):
        mb = mainboard.get(oracle_id, {"name": None, "decks": set()})
        sl = shortlist.get(oracle_id, {"name": None, "decks": set()})
        name = mb["name"] or sl["name"]
        if not name:
            continue
        demand = len(mb["decks"]) + len(sl["decks"])
        owned_qty = owned.get(oracle_id, 0)
        shortfall = demand - owned_qty
        if shortfall <= 0:
            continue
        mainboard_deck_ids = sorted(mb["decks"], key=lambda d: deck_id_to_name.get(d, "?").casefold())
        out.append({
            "oracle_id": oracle_id,
            "card": name,
            "owned_qty": owned_qty,
            "demand": demand,
            "shortfall": shortfall,
            "mainboard_decks": [deck_id_to_name.get(d, "?") for d in mainboard_deck_ids],
            # {deck_id: name} rather than a bare id list, so a caller can
            # offer "drop from deck N" (writes.remove_deck_card needs the
            # deck_id, not just its display name).
            "mainboard_deck_ids": {d: deck_id_to_name.get(d, "?") for d in mainboard_deck_ids},
            "shortlist_decks": sorted(deck_id_to_name.get(d, "?") for d in sl["decks"]),
        })
    out.sort(key=lambda r: (-r["shortfall"], r["card"].casefold()))
    return out


def deck_card_scryfall_id_for_oracle(conn, deck_id, oracle_id):
    """The scryfall_id a deck's mainboard actually uses for an oracle card
    — what writes.remove_deck_card() needs (it's keyed by printing, not
    oracle_id) to act on a contention_table() row's "drop from deck N"."""
    row = conn.execute(
        """SELECT dc.scryfall_id FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
           WHERE dc.deck_id = ? AND c.oracle_id = ? LIMIT 1""",
        (deck_id, oracle_id),
    ).fetchone()
    return row[0] if row else None


def deck_card_entry(conn, deck_id, scryfall_id):
    """{"name", "quantity"} for one printing as this deck sees it, or None
    if the printing isn't in `cards` at all. quantity is None when the card
    exists but isn't on the deck's mainboard. Read fresh by the Board's
    edit handler (deck_board.apply_board_action) so a quantity bump works
    off the database, never a stale browser payload."""
    row = conn.execute(
        """SELECT c.name, dc.quantity FROM cards c
           LEFT JOIN deck_cards dc ON dc.scryfall_id = c.scryfall_id AND dc.deck_id = ?
           WHERE c.scryfall_id = ?""",
        (deck_id, scryfall_id),
    ).fetchone()
    return {"name": row[0], "quantity": row[1]} if row else None


# ------------------------------------------------------------------
# Reads behind writes.sleeve_into_deck() / unsleeve_from_deck()
# (EDIT_FLOW_PLAN.md Phase 1). Both match across printings — oracle_id,
# falling back to the exact name — like every other ownership question
# here, so a different printing sleeved in the deck still counts.
# ------------------------------------------------------------------
def card_lots(conn, scryfall_id):
    """Every collection lot of a card, across printings:
    [(collection_id, quantity, location, scryfall_id), ...] in
    collection_id order. `quantity` is COALESCEd to 1 (a NULL lot is at
    least one copy — see _lots_for_card); `location` is as stored, not
    stripped, so a move can be undone back to exactly where it was."""
    name_row = conn.execute("SELECT name FROM cards WHERE scryfall_id=?", (scryfall_id,)).fetchone()
    ids = [r[0] for r in _lots_for_card(conn, name=name_row[0] if name_row else None,
                                        scryfall_id=scryfall_id)]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    return [tuple(r) for r in conn.execute(
        f"""SELECT collection_id, COALESCE(quantity, 1), location, scryfall_id
            FROM collection WHERE collection_id IN ({marks}) ORDER BY collection_id""",
        ids,
    ).fetchall()]


def deck_list_quantity(conn, deck_id, scryfall_id):
    """How many copies of a card a deck's list runs, summed over every
    printing of it on the list — what "needed" means to the move helpers,
    so swapping one printing for another needs no copy moved."""
    oracle_ids = oracle_ids_for_card(conn, scryfall_id=scryfall_id)
    if oracle_ids:
        where, params = f"c.oracle_id IN ({','.join('?' for _ in oracle_ids)})", tuple(oracle_ids)
    else:
        where, params = "c.name = (SELECT name FROM cards WHERE scryfall_id = ?) COLLATE NOCASE", (scryfall_id,)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(COALESCE(dc.quantity, 1)), 0)
            FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
            WHERE dc.deck_id = ? AND {where}""",
        (deck_id, *params),
    ).fetchone()
    return int(row[0] or 0)


# ------------------------------------------------------------------
# Where a copy can come from (EDIT_FLOW_PLAN.md Phase 2). One set of reads
# behind the source picker, the Board payload, writes.sleeve_into_deck()'s
# Auto rule and the allocator, so what the picker offers, what Auto picks
# and what actually moves can't drift apart.
# ------------------------------------------------------------------
# Where a removed copy goes when nothing says otherwise. writes.py's
# DEFAULT_LOCATION is the same value (writes can't be imported from here).
DEFAULT_STORAGE = "Box"

# The `deck_changes.source` values a caller may set (NULL is Auto): a storage
# Location (Phase 2) or another deck (Phase 3, source_location = its name).
# 'buy' (Phase 4) is added here when it exists, so a write that names it now
# is refused instead of saved and ignored.
SOURCES = ("storage", "deck")


def same_location(a, b):
    """Two storage Locations are the same place if they match ignoring
    case and surrounding spaces ("lands box" is "Lands Box"). Deck names
    are still matched exactly, as everywhere else."""
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def storage_locations(conn):
    """Every storage Location a copy can be taken from or put away to: the
    catalog plus "Box" (always offered, whatever the catalog holds), minus
    deck names — a deck as a source or destination is Phase 3's explicit
    transfer, not a storage choice. Box first, then A-Z."""
    found = {r[0] for r in conn.execute("SELECT location FROM location_catalog").fetchall()}
    found.add(DEFAULT_STORAGE)
    found -= deck_name_set(conn)
    return sorted(found, key=lambda loc: (not same_location(loc, DEFAULT_STORAGE), loc.casefold()))


def free_lots(conn, scryfall_id, location=None):
    """Lots of a card (any printing) that are free to take: sleeved in no
    deck, unlocated ones included, with copies in them. Biggest lot first —
    the order plan_availability() allocates in and sleeve_into_deck() takes
    in, so the copy the Workbench promised is the one that moves.
    `location` narrows it to one storage Location (same_location()).
    [(collection_id, quantity, location, scryfall_id), ...]"""
    deck_names = deck_name_set(conn)
    lots = [lot for lot in card_lots(conn, scryfall_id)
            if (lot[2] or "").strip() not in deck_names and lot[1] > 0]
    if location is not None:
        lots = [lot for lot in lots if same_location(lot[2], location)]
    return sorted(lots, key=lambda lot: (-lot[1], lot[0]))


def card_sources(conn, scryfall_id, deck_id):
    """The options for where one card could come from, for the source
    picker and the Board payload. Across every printing (oracle_id), like
    every other ownership question here. Returns:

      here      copies already sleeved in THIS deck (Auto counts them first)
      storage   [{"location", "quantity"}, ...] free copies per storage
                Location, biggest first — what a "from storage" pick can
                name. Unlocated lots are left out: there's no place to name
                (they're counted in `unlocated` and Auto still uses them)
      unlocated free copies with no Location recorded
      decks     [{"deck", "quantity", "listed"}, ...] every other deck that
                holds a sleeved copy (`quantity`) or runs the card on its list
                (`listed`, across printings). Phase 3: a deck with `listed`
                is a choosable source — taking it changes that deck's list
                too — and it can give copies only once it's fully sleeved
                (quantity >= listed), since the move helpers take only what
                a list no longer needs
      price     the printing's current USD price, or None (Phase 4's Buy)
      bulk      True for an untracked basic land: nothing is ever moved
      auto      what Auto would do right now: {"kind": "here" | "storage" |
                "new" | "bulk", "location": Location or None}. "new" is a
                starter lot — a copy you don't own yet
    """
    deck_names = deck_name_set(conn)
    deck_row = conn.execute("SELECT name FROM decks WHERE deck_id=?", (deck_id,)).fetchone()
    deck_name = deck_row[0] if deck_row else None
    here, others, per_location, unlocated = 0, {}, {}, 0
    for _cid, qty, loc, _sid in card_lots(conn, scryfall_id):
        place = (loc or "").strip()
        if deck_name is not None and place == deck_name:
            here += qty
        elif place in deck_names:
            others[place] = others.get(place, 0) + qty
        elif qty > 0 and not place:
            unlocated += qty
        elif qty > 0:
            per_location[place] = per_location.get(place, 0) + qty
    bulk = is_bulk_basic(conn, scryfall_id=scryfall_id)
    free = free_lots(conn, scryfall_id)
    if bulk:
        auto = {"kind": "bulk", "location": None}
    elif here:
        auto = {"kind": "here", "location": deck_name}
    elif free:
        auto = {"kind": "storage", "location": (free[0][2] or "").strip() or None}
    else:
        auto = {"kind": "new", "location": None}
    price_row = conn.execute(
        "SELECT current_price_usd FROM cards WHERE scryfall_id=?", (scryfall_id,)
    ).fetchone()
    # What every other deck's list runs, across printings — deck_list_quantity()'s
    # match, in one grouped query rather than one per deck.
    oracle_ids = oracle_ids_for_card(conn, scryfall_id=scryfall_id)
    if oracle_ids:
        where, params = f"c.oracle_id IN ({','.join('?' for _ in oracle_ids)})", tuple(oracle_ids)
    else:
        where, params = "c.name = (SELECT name FROM cards WHERE scryfall_id = ?) COLLATE NOCASE", (scryfall_id,)
    listed = {name: int(n) for name, n in conn.execute(
        f"""SELECT d.name, SUM(COALESCE(dc.quantity, 1))
            FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
            JOIN decks d ON d.deck_id = dc.deck_id
            WHERE dc.deck_id IS NOT ? AND {where}
            GROUP BY d.deck_id""",
        (deck_id, *params),
    ).fetchall() if n}
    return {
        "here": here,
        "storage": [{"location": loc, "quantity": n}
                    for loc, n in sorted(per_location.items(), key=lambda kv: (-kv[1], kv[0].casefold()))],
        "unlocated": unlocated,
        "decks": [{"deck": d, "quantity": others.get(d, 0), "listed": listed.get(d, 0)}
                  for d in sorted(set(others) | set(listed), key=str.casefold)],
        "price": price_row[0] if price_row else None,
        "bulk": bulk,
        "auto": auto,
    }


def brewing_claims(conn):
    """Copies a BREWING deck has promised itself from a chosen storage
    Location but not pulled yet — what plan_availability() counts as already
    taken, so a swap planned elsewhere can't promise the same copy.

    Only a saved CHOICE claims anything: a brewing card left on Auto claims
    nothing (as before Phase 2), because it hasn't picked a copy. The choice
    is the card's latest applied add row (a direct edit logs one; so does
    applying an idea). Returns [{"deck_id", "deck_name", "scryfall_id",
    "card", "quantity", "location"}, ...] with quantity = copies the list
    runs minus copies already sleeved there."""
    latest = {}
    for deck_id, sid, source, location in conn.execute(
        """SELECT dc.deck_id, dc.add_scryfall_id, dc.source, dc.source_location
           FROM deck_changes dc JOIN decks d ON d.deck_id = dc.deck_id
           WHERE d.build_state = 'brewing' AND dc.status = 'applied'
             AND dc.add_scryfall_id IS NOT NULL
           ORDER BY dc.change_id"""
    ).fetchall():
        latest[(deck_id, sid)] = (source, location)
    claims = []
    for deck_id, deck_name, sid, qty, card in conn.execute(
        """SELECT d.deck_id, d.name, dcd.scryfall_id, dcd.quantity, c.name
           FROM decks d JOIN deck_cards dcd ON dcd.deck_id = d.deck_id
           JOIN cards c ON c.scryfall_id = dcd.scryfall_id
           WHERE d.build_state = 'brewing'"""
    ).fetchall():
        source, location = latest.get((deck_id, sid), (None, None))
        if source != "storage" or not (location or "").strip():
            continue
        sleeved = sum(n for _c, n, loc, _s in card_lots(conn, sid) if (loc or "").strip() == deck_name)
        owed = min(int(qty or 1), deck_list_quantity(conn, deck_id, sid) - sleeved)
        if owed > 0:
            claims.append({"deck_id": deck_id, "deck_name": deck_name, "scryfall_id": sid,
                           "card": card, "quantity": owed, "location": location.strip()})
    return claims


# ------------------------------------------------------------------
# Linked pairs (EDIT_FLOW_PLAN.md Phase 3): a card taken from another deck
# is a removal on the donor (add side empty, dest_location = the receiving
# deck) and an add on the receiver (source='deck', source_location = the
# donor), each naming the other in linked_change_id. The donor's half is
# the one with no add — a receiver always adds.
# ------------------------------------------------------------------
def is_donor_row(row):
    """True for the donor's half of a pair: a removal with no add side."""
    return (bool(row.get("remove_scryfall_id")) and not row.get("add_scryfall_id")
            and not (row.get("add_name") or "").strip())


def open_takes(conn, deck_id=None, oracle_id=None):
    """Every OPEN donor removal that is half of a pair — a copy promised to
    another deck and not handed over yet — oldest first. The receiver may be
    open too (a staged take) or already applied (a brewing deck's take,
    whose list changed at once: "lopsided" in the plan). `deck_id` narrows
    it to one donor deck; `oracle_id` to one card. Each row:

      {donor_change_id, donor_deck_id, donor_deck_name, scryfall_id (the
       donor list's printing), card, quantity, donor_status,
       receiver_change_id, receiver_deck_id, receiver_deck_name,
       receiver_status, receiver_scryfall_id, oracle_id, planned_at,
       created_at}"""
    sql = """SELECT d.change_id, d.deck_id, dd.name, d.remove_scryfall_id,
                    COALESCE(rc.name, d.remove_name), d.quantity, d.status,
                    r.change_id, r.deck_id, rd.name, r.status, r.add_scryfall_id,
                    rc.oracle_id, d.planned_at, d.created_at
             FROM deck_changes d
             JOIN decks dd ON dd.deck_id = d.deck_id
             JOIN deck_changes r ON r.change_id = d.linked_change_id
             JOIN decks rd ON rd.deck_id = r.deck_id
             LEFT JOIN cards rc ON rc.scryfall_id = d.remove_scryfall_id
             WHERE d.status IN ('idea','planned') AND r.status IN ('idea','planned','applied')
               AND d.add_scryfall_id IS NULL AND COALESCE(TRIM(d.add_name), '') = ''
               AND d.remove_scryfall_id IS NOT NULL"""
    params = []
    if deck_id is not None:
        sql += " AND d.deck_id = ?"
        params.append(deck_id)
    if oracle_id is not None:
        sql += " AND rc.oracle_id = ?"
        params.append(oracle_id)
    sql += " ORDER BY COALESCE(d.planned_at, d.created_at), d.change_id"
    keys = ("donor_change_id", "donor_deck_id", "donor_deck_name", "scryfall_id", "card",
            "quantity", "donor_status", "receiver_change_id", "receiver_deck_id",
            "receiver_deck_name", "receiver_status", "receiver_scryfall_id", "oracle_id",
            "planned_at", "created_at")
    return [dict(zip(keys, r)) for r in conn.execute(sql, params).fetchall()]


def deck_leaving(conn, deck_id):
    """{scryfall_id on this deck's list: [receiving deck names]} for the
    Board's "leaving → Prosper" badge: every open take of one of this deck's
    cards. Keyed by the printing the removal names (the donor's own list
    printing), the way deck_board.planned_cuts() keys its "cut for" badge."""
    out = {}
    for take in open_takes(conn, deck_id=deck_id):
        out.setdefault(take["scryfall_id"], []).append(take["receiver_deck_name"])
    return out


def collapse_linked_pairs(rows):
    """The Workbench's planned queue with each linked pair as ONE row.

    `rows` is open_changes_dataframe()'s planned rows (a DataFrame or dicts,
    with its linked_* columns). A donor removal whose partner is open or
    already applied is folded into its pair and shown under the RECEIVING
    deck — the deck gaining the card is where the decision lives, and its
    side may not be in `rows` at all (an idea, or a brewing deck's add that
    is already applied). Every other row passes through as itself.

    Returns [{"group_deck_id", "group_deck_name", "change_id", "row",
    "pair"}] in input order. `change_id` is the row an action should name
    (applying or dropping either side handles both): the receiver's when
    it is planned, else the donor's. `pair` is None, or {"card",
    "from_deck", "to_deck", "receiver_status"}."""
    records = _rows_of(rows)
    by_id = {int(r["change_id"]): r for r in records}
    out, seen = [], set()
    for r in records:
        cid = int(r["change_id"])
        if cid in seen:
            continue
        linked = r.get("linked_change_id")
        linked = int(linked) if linked is not None else None
        if linked is None or r.get("linked_status") not in ("idea", "planned", "applied"):
            out.append({"group_deck_id": r["deck_id"], "group_deck_name": r["deck_name"],
                        "change_id": cid, "row": r, "pair": None})
            continue
        seen.update({cid, linked})
        partner = by_id.get(linked)
        donor, receiver = (r, partner) if is_donor_row(r) else (partner, r)
        if receiver is not None:
            to_id, to_name, to_status = receiver["deck_id"], receiver["deck_name"], receiver["status"]
        else:
            to_id, to_name, to_status = r.get("linked_deck_id"), r.get("linked_deck_name"), r.get("linked_status")
        from_deck = donor["deck_name"] if donor is not None else r.get("linked_deck_name")
        card = (receiver or {}).get("add_card") or (donor or {}).get("remove_card")
        acting = receiver if receiver is not None and receiver["status"] == "planned" else (donor or r)
        out.append({"group_deck_id": to_id, "group_deck_name": to_name,
                    "change_id": int(acting["change_id"]), "row": acting,
                    "pair": {"card": card, "from_deck": from_deck, "to_deck": to_name,
                             "receiver_status": to_status}})
    return out


# ------------------------------------------------------------------
# The move log (EDIT_FLOW_PLAN.md Phase 6; table and column meanings in
# schema.sql). Written only by writes.py's lot writers.
# ------------------------------------------------------------------
def last_move_id(conn):
    """The newest move_id in the log (0 when empty) — taken before a
    collection-side save, so deck_sync_offers() can ask what THAT save
    moved and nothing older."""
    return conn.execute("SELECT COALESCE(MAX(move_id), 0) FROM collection_moves").fetchone()[0]


def collection_moves_for_card(conn, name, limit=25):
    """The move log for one card by name (newest first), across printings
    and including lots since deleted — card_name is copied at write time.
    Each row: {move_id, collection_id, quantity, from_location,
    to_location, reason, change_id, moved_at}."""
    rows = conn.execute(
        """SELECT move_id, collection_id, quantity, from_location, to_location,
                  reason, change_id, moved_at
           FROM collection_moves WHERE card_name = ? COLLATE NOCASE
           ORDER BY move_id DESC LIMIT ?""",
        ((name or "").strip(), int(limit)),
    ).fetchall()
    keys = ("move_id", "collection_id", "quantity", "from_location", "to_location",
            "reason", "change_id", "moved_at")
    return [dict(zip(keys, r)) for r in rows]


def deck_list_printing(conn, deck_id, scryfall_id):
    """(scryfall_id, quantity) of the printing of this card the deck's list
    already runs — the biggest, if it runs two — or None when the card isn't
    on the list at all. Matched across printings like deck_list_quantity()."""
    oracle_ids = oracle_ids_for_card(conn, scryfall_id=scryfall_id)
    if oracle_ids:
        where, params = f"c.oracle_id IN ({','.join('?' for _ in oracle_ids)})", tuple(oracle_ids)
    else:
        where, params = "c.name = (SELECT name FROM cards WHERE scryfall_id = ?) COLLATE NOCASE", (scryfall_id,)
    row = conn.execute(
        f"""SELECT dc.scryfall_id, COALESCE(dc.quantity, 1)
            FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
            WHERE dc.deck_id = ? AND {where}
            ORDER BY COALESCE(dc.quantity, 1) DESC, dc.scryfall_id LIMIT 1""",
        (deck_id, *params),
    ).fetchone()
    return (row[0], row[1]) if row else None


def deck_sync_offers(conn, since_move_id):
    """What a collection-side save did to decks' sleeves, as list changes
    to offer (EDIT_FLOW_PLAN.md Phase 6). Reads the moves logged after
    `since_move_id` and, per deck and card, nets the copies that came in
    against those that left. An offer is made only where the deck's list
    does NOT already agree with its sleeves, and never for more copies than
    the save itself moved:

      add     copies now sleeved in the deck that its list doesn't run
      remove  the list runs more than is now sleeved, after copies left

    Untracked bulk basics are skipped (nobody lists a Swamp per lot).
    Returns [{deck_id, deck_name, scryfall_id, card, action, quantity,
    list_quantity, new_quantity}] — plain data, so it can wait in session
    state for the dialog. writes.sync_deck_lists() applies them."""
    decks = {name: deck_id for deck_id, name in conn.execute("SELECT deck_id, name FROM decks").fetchall()}
    net = {}  # (deck_name, card_name) -> [scryfall_id, copies in minus copies out]
    for sid, card, qty, frm, to in conn.execute(
        """SELECT scryfall_id, card_name, quantity, from_location, to_location
           FROM collection_moves WHERE move_id > ? ORDER BY move_id""",
        (since_move_id,),
    ).fetchall():
        copies = qty if qty is not None else 1
        for deck_name, sign in ((to, 1), (frm, -1)):
            if deck_name in decks and (frm or "") != (to or ""):
                entry = net.setdefault((deck_name, card or sid), [sid, 0])
                entry[1] += sign * copies

    offers = []
    for (deck_name, card), (sid, delta) in net.items():
        if delta == 0 or not sid or is_bulk_basic(conn, scryfall_id=sid):
            continue
        deck_id = decks[deck_name]
        sleeved = sum(qty for _cid, qty, loc, _s in card_lots(conn, sid) if (loc or "").strip() == deck_name)
        listed = deck_list_quantity(conn, deck_id, sid)
        on_list = deck_list_printing(conn, deck_id, sid)
        if delta > 0:
            n, action = min(delta, sleeved - listed), "add"
            target = on_list[0] if on_list else sid
            new_quantity = (on_list[1] if on_list else 0) + n
        else:
            n, action = min(-delta, listed - sleeved), "remove"
            if not on_list:
                continue
            target = on_list[0]
            n = min(n, on_list[1])
            new_quantity = on_list[1] - n
        if n > 0:
            offers.append({
                "deck_id": deck_id, "deck_name": deck_name, "scryfall_id": target, "card": card,
                "action": action, "quantity": n, "list_quantity": listed, "new_quantity": new_quantity,
            })
    return sorted(offers, key=lambda o: (o["deck_name"].casefold(), o["card"].casefold()))


def buy_list(conn, contention=None):
    """The cross-deck shopping list: every open **idea** (not yet staged —
    a planned swap that's not owned buys a starter lot automatically on
    apply, it isn't a decision pending here) whose card has ZERO owned
    copies, grouped across decks so wanting the same card on three
    shortlists prices it once, not three times. Plus one row per
    contention_table() shortfall — "you already decided you want this
    twice" buys, priced at the shortfall quantity.

    `contention` reuses an existing contention_table() result instead of
    recomputing it. Returns {"rows": [...], "total": float}, where each row
    is {card, quantity, price, subtotal, decks, reason} and `reason` is
    "idea" or "contention"."""
    if contention is None:
        contention = contention_table(conn)

    groups = {}
    for deck_name, add_sid, add_name, qty, add_oracle_id, resolved_name, price in conn.execute(
        """SELECT d.name, dc.add_scryfall_id, dc.add_name, dc.quantity, ac.oracle_id,
                  ac.name,
                  COALESCE(ac.current_price_usd,
                           (SELECT MIN(p.current_price_usd) FROM cards p
                            WHERE p.name = dc.add_name COLLATE NOCASE))
           FROM deck_changes dc
           JOIN decks d ON d.deck_id = dc.deck_id
           LEFT JOIN cards ac ON ac.scryfall_id = dc.add_scryfall_id
           WHERE dc.status = 'idea'
             AND (dc.add_scryfall_id IS NOT NULL OR dc.add_name IS NOT NULL)"""
    ).fetchall():
        name = resolved_name or add_name
        if not name:
            continue
        owned = sum(q or 0 for _cid, q, _loc in _lots_for_card(conn, name=name, scryfall_id=add_sid))
        if owned > 0:
            continue
        key = add_oracle_id or f"name:{name.casefold()}"
        g = groups.setdefault(key, {"card": name, "price": price, "quantity": 0, "decks": set()})
        g["quantity"] += max(1, int(qty or 1))
        g["decks"].add(deck_name)
        if g["price"] is None:
            g["price"] = price

    rows, total = [], 0.0
    for g in sorted(groups.values(), key=lambda g: g["card"].casefold()):
        subtotal = (g["price"] or 0) * g["quantity"]
        total += subtotal
        rows.append({
            "card": g["card"], "quantity": g["quantity"], "price": g["price"],
            "subtotal": subtotal, "decks": sorted(g["decks"]), "reason": "idea",
        })

    for c in contention:
        price_row = conn.execute(
            "SELECT MIN(current_price_usd) FROM cards WHERE oracle_id = ?", (c["oracle_id"],)
        ).fetchone()
        price = price_row[0] if price_row else None
        subtotal = (price or 0) * c["shortfall"]
        total += subtotal
        rows.append({
            "card": c["card"], "quantity": c["shortfall"], "price": price,
            "subtotal": subtotal, "decks": c["mainboard_decks"] + c["shortlist_decks"],
            "reason": "contention",
        })

    return {"rows": rows, "total": total}


def card_lookup(conn, name):
    """Everything known about one card, rolled up by oracle_id across every
    owned printing — the view the data shape kept asking for (Card Lookup
    page): price/rarity/type from a representative printing, every owned
    lot, every deck it's mainboarded in, every open shortlist entry for it,
    and its applied-change history.

    Returns None if `name` doesn't match any printing in the local
    database. `name` is matched exactly (case-insensitive) against
    cards.name — card_printings_by_name()'s existing contract."""
    name = (name or "").strip()
    if not name:
        return None
    printings = card_printings_by_name(conn, name)
    if not printings:
        return None
    rep_sid = max(printings, key=lambda p: p["current_price_usd"] or 0)["scryfall_id"]
    card_cols = [
        "scryfall_id", "name", "set_code", "collector_number", "type_line",
        "mana_cost", "cmc", "color_identity", "oracle_text", "rarity", "image_uri",
        "local_image_path", "is_basic_land", "is_game_changer", "is_reserved",
        "is_showcase", "is_borderless", "commander_legal", "current_price_usd",
    ]
    card_row = conn.execute(
        f"SELECT {', '.join(card_cols)} FROM cards WHERE scryfall_id = ?", (rep_sid,)
    ).fetchone()
    card = dict(zip(card_cols, card_row))

    oracle_ids = oracle_ids_for_card(conn, name=name)
    oracle_id = sorted(oracle_ids)[0] if oracle_ids else None

    deck_id_to_name = {r[0]: r[1] for r in conn.execute("SELECT deck_id, name FROM decks").fetchall()}

    owned_qty = 0
    lots = []
    for collection_id, quantity, location in _lots_for_card(conn, name=name):
        owned_qty += quantity
        lots.append({"collection_id": collection_id, "quantity": quantity, "location": location})

    if oracle_id:
        mb_rows = conn.execute(
            """SELECT dc.deck_id, dc.quantity
               FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
               WHERE c.oracle_id = ?""",
            (oracle_id,),
        ).fetchall()
    else:
        mb_rows = conn.execute(
            """SELECT dc.deck_id, dc.quantity
               FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
               WHERE c.name = ? COLLATE NOCASE""",
            (name,),
        ).fetchall()
    mainboard_in = [
        {"deck_id": d, "deck_name": deck_id_to_name.get(d, "?"), "quantity": qty}
        for d, qty in mb_rows
    ]

    shortlisted_in = []
    for change_id, deck_id, status, add_sid, add_name, add_oracle_id, source, source_location in conn.execute(
        """SELECT dc.change_id, dc.deck_id, dc.status, dc.add_scryfall_id, dc.add_name, ac.oracle_id,
                  dc.source, dc.source_location
           FROM deck_changes dc LEFT JOIN cards ac ON ac.scryfall_id = dc.add_scryfall_id
           WHERE dc.status IN ('idea', 'planned')"""
    ).fetchall():
        row_oracle_id, _n, _b = _resolve_change_oracle(conn, add_sid, add_name, add_oracle_id)
        is_match = (
            (oracle_id and row_oracle_id == oracle_id)
            or (not oracle_id and not row_oracle_id and (add_name or "").casefold() == name.casefold())
        )
        if is_match:
            shortlisted_in.append({
                "change_id": change_id, "deck_id": deck_id,
                "deck_name": deck_id_to_name.get(deck_id, "?"), "status": status,
                "source": source, "source_location": source_location,
            })

    history = []
    if oracle_id:
        for (change_id, deck_id, status, resolved_at, add_card, remove_card,
             source, source_location, dest) in conn.execute(
            """SELECT dc.change_id, dc.deck_id, dc.status, dc.resolved_at,
                      COALESCE(ac.name, dc.add_name) AS add_card,
                      COALESCE(rc.name, dc.remove_name) AS remove_card,
                      dc.source, dc.source_location, dc.dest_location
               FROM deck_changes dc
               LEFT JOIN cards ac ON ac.scryfall_id = dc.add_scryfall_id
               LEFT JOIN cards rc ON rc.scryfall_id = dc.remove_scryfall_id
               WHERE dc.status = 'applied'
                 AND (dc.add_scryfall_id IN (SELECT scryfall_id FROM cards WHERE oracle_id = :oid)
                   OR dc.remove_scryfall_id IN (SELECT scryfall_id FROM cards WHERE oracle_id = :oid))
               ORDER BY dc.resolved_at DESC, dc.change_id DESC LIMIT 10""",
            {"oid": oracle_id},
        ).fetchall():
            history.append({
                "change_id": change_id, "deck_id": deck_id,
                "deck_name": deck_id_to_name.get(deck_id, "?"), "status": status,
                "resolved_at": resolved_at, "add_card": add_card, "remove_card": remove_card,
                "source": source, "source_location": source_location, "dest_location": dest,
            })

    demand = len(mainboard_in) + len(shortlisted_in)
    return {
        "name": card["name"], "oracle_id": oracle_id, "card": card,
        "owned_qty": owned_qty, "lots": lots,
        "mainboard_in": mainboard_in, "shortlisted_in": shortlisted_in,
        "demand": demand, "shortfall": max(0, demand - owned_qty),
        "history": history,
        # Phase 6: where each copy went (newest first), beside the list history above.
        "moves": collection_moves_for_card(conn, card["name"]),
        # Phase 3: copies promised from one deck to another, not handed over yet.
        "pending_takes": ([{"from_deck": t["donor_deck_name"], "to_deck": t["receiver_deck_name"],
                            "quantity": t["quantity"], "receiver_status": t["receiver_status"]}
                           for t in open_takes(conn, oracle_id=oracle_id)] if oracle_id else []),
    }


# ------------------------------------------------------------------
# Change history, deck comparison, tile status (Workbench rework, Phase 5)
# ------------------------------------------------------------------

def _games_between(conn, deck_id, after_date, through_date=None):
    """(games, wins, losses) for one deck's logged games dated strictly
    after `after_date` and, if given, on or before `through_date`.

    `games.date` carries a day and no time, so a game on the very day a
    change was applied can't be placed before or after it. The rule is
    fixed rather than guessed: a game belongs to the deck version made by
    the latest change dated *strictly before* it. Same-day games therefore
    count against the older list, never the newer one — the version they
    can be proven to have used."""
    sql = """SELECT COUNT(*), COALESCE(SUM(gp.is_winner), 0)
             FROM game_participants gp JOIN games g ON g.game_id = gp.game_id
             WHERE gp.deck_id = ? AND g.date IS NOT NULL AND g.date > ?"""
    params = [deck_id, after_date]
    if through_date is not None:
        sql += " AND g.date <= ?"
        params.append(through_date)
    n, wins = conn.execute(sql, params).fetchone()
    return int(n), int(wins), int(n) - int(wins)


def deck_change_history(conn, deck_id):
    """One deck's resolved changes as a newest-first timeline, with the
    games played between them — the correlation that was impossible while
    `execute_swap()` left no trace.

    Returns a list of dicts of two kinds:

      {"kind": "change", "change_id", "status" ('applied'|'dropped'), "date",
       "add_card", "remove_card", "quantity", "notes", "source", "source_location",
       "dest_location"}
      {"kind": "games", "games", "wins", "losses", "label"}

    A "games" row sits where the deck was played as-is: above the newest
    applied change (games since it, up to today) and between two applied
    changes on different dates (games between them). Dropped rows never
    start a new period — rejecting a card doesn't change the deck — and
    games dated before the oldest applied change are omitted, since history
    only starts when logging did and there's no list to attribute them to.

    `date` is the day part of `resolved_at`, which SQLite stamps in UTC; a
    change applied late in the evening local time can read as the next day.
    Games are compared by day, so that skew only matters for a game logged
    on that borderline day. Empty list for a deck with no resolved rows."""
    rows = conn.execute(
        """SELECT dc.change_id, dc.status,
                  COALESCE(dc.resolved_at, dc.created_at) AS at,
                  COALESCE(ac.name, dc.add_name)    AS add_card,
                  COALESCE(rc.name, dc.remove_name) AS remove_card,
                  dc.quantity, dc.notes, dc.source, dc.source_location, dc.dest_location
           FROM deck_changes dc
           LEFT JOIN cards ac ON ac.scryfall_id = dc.add_scryfall_id
           LEFT JOIN cards rc ON rc.scryfall_id = dc.remove_scryfall_id
           WHERE dc.deck_id = ? AND dc.status IN ('applied', 'dropped')
           ORDER BY at DESC, dc.change_id DESC""",
        (deck_id,),
    ).fetchall()
    if not rows:
        return []

    def day(ts):
        return str(ts)[:10]

    def games_row(games, wins, losses, label):
        return {"kind": "games", "games": games, "wins": wins, "losses": losses, "label": label}

    timeline = []
    newest_applied = next((day(r[2]) for r in rows if r[1] == "applied"), None)
    if newest_applied is not None:
        timeline.append(games_row(*_games_between(conn, deck_id, newest_applied), "since the latest change"))

    prev_applied = newest_applied
    for change_id, status, at, add_card, remove_card, qty, notes, source, source_location, dest in rows:
        d = day(at)
        if status == "applied":
            if d < prev_applied:
                # Moving to an older applied date: the games between the two
                # versions go here, in the gap between their change groups.
                timeline.append(games_row(*_games_between(conn, deck_id, d, prev_applied), "between these changes"))
            prev_applied = d
        timeline.append({
            "kind": "change", "change_id": change_id, "status": status, "date": d,
            "add_card": add_card, "remove_card": remove_card, "quantity": qty, "notes": notes,
            "source": source, "source_location": source_location, "dest_location": dest,
        })
    return timeline


def deck_comparison(conn):
    """One row per deck for the Decks page's comparison matrix and the home
    tiles' status strips — the shape of the whole pod, which per-deck pages
    structurally can't show. Columns: deck_id, name, bracket, interaction,
    avg_cmc (non-land mainboard, quantity-weighted — the same land exclusion
    as deck_mana_curve), value, games, wins, losses, win_rate (None with no
    games), game_changers, combos (None if never checked against Spellbook,
    which is not the same as 0), planned, ideas, build_pct (None for a deck
    with nothing to sleeve — see deck_reconciliation), build_state (Phase 6:
    NULL for a normal built deck)."""
    df = pd.read_sql_query(
        """SELECT d.deck_id, d.name, d.bracket, d.interaction,
                  (SELECT SUM(dc.quantity * c.cmc) * 1.0 / NULLIF(SUM(dc.quantity), 0)
                     FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
                    WHERE dc.deck_id = d.deck_id AND c.type_line NOT LIKE '%Land%') AS avg_cmc,
                  COALESCE(v.total_value, 0) AS value,
                  COALESCE(s.games_played, 0) AS games,
                  COALESCE(s.wins, 0) AS wins,
                  COALESCE(s.losses, 0) AS losses,
                  s.win_rate AS win_rate,
                  (SELECT COUNT(*) FROM deck_game_changers g WHERE g.deck_id = d.deck_id) AS game_changers,
                  cs.included_count AS combos,
                  (SELECT COUNT(*) FROM deck_changes x
                    WHERE x.deck_id = d.deck_id AND x.status = 'planned') AS planned,
                  (SELECT COUNT(*) FROM deck_changes x
                    WHERE x.deck_id = d.deck_id AND x.status = 'idea') AS ideas,
                  d.build_state
           FROM decks d
           LEFT JOIN deck_value v ON v.deck_id = d.deck_id
           LEFT JOIN deck_stats s ON s.deck_id = d.deck_id
           LEFT JOIN deck_combo_sync cs ON cs.deck_id = d.deck_id
           ORDER BY d.name COLLATE NOCASE""",
        conn,
    )
    pct = {r["deck_id"]: r["build"]["pct"] for r in deck_reconciliation(conn)}
    df["build_pct"] = df["deck_id"].map(pct)
    return df


# ------------------------------------------------------------------
# Collection
# ------------------------------------------------------------------
def collection_dataframe(conn):
    """One row per collection LOT (collection_id) — i.e. the real schema
    grain, preserving per-lot location/price/date rather than silently
    aggregating rows that may live in different boxes or decks.

    `in_deck` (Prompt Pass 4) is 1 if this printing's scryfall_id is used
    in ANY deck's mainboard (deck_cards), 0 otherwise — the same
    "spoken for by a deck" test writes.prune_collection() already uses,
    reused here to power the Collection page's deck-status filter."""
    return pd.read_sql_query(
        f"""SELECT col.collection_id, {CARD_COLUMNS},
                   col.quantity, col.foil, col.location, col.date_acquired,
                   col.price_paid, col.source,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM deck_cards dc WHERE dc.scryfall_id = col.scryfall_id
                   ) THEN 1 ELSE 0 END AS in_deck
            FROM collection col
            JOIN cards c ON c.scryfall_id = col.scryfall_id
            ORDER BY c.name COLLATE NOCASE""",
        conn,
    )


def collection_summary(conn):
    row = conn.execute(
        """SELECT COUNT(*) AS lots,
                  COUNT(DISTINCT scryfall_id) AS unique_printings,
                  SUM(quantity) AS tracked_quantity,
                  SUM(quantity * (SELECT current_price_usd FROM cards
                                   WHERE cards.scryfall_id = collection.scryfall_id)) AS tracked_value
           FROM collection"""
    ).fetchone()
    return dict(row) if row else {}


# ------------------------------------------------------------------
# Game Changers / Mana Tags overviews (Card Database tabs) — UI plan
# Phase 8. The tag tables (game_changer_tags, mana_tags) ARE the list:
# they're hand-maintained and keyed by card NAME, so every tagged name
# shows whether or not it has a `cards` row. `cards` only decorates it
# (art, Scryfall flag) via a LEFT JOIN onto one representative printing,
# and owned/deck counts match by NAME across printings. Game Changers also
# keep every Scryfall-flagged card in the list (a newly flagged card
# appears untagged), which is what makes the "Scryfall says" column useful:
# tagged-but-not-flagged is the signal for stale entries.
# ------------------------------------------------------------------
def _tag_list_overview(conn, tag_table, with_scryfall_flag):
    flagged_names = (
        "UNION SELECT name FROM cards WHERE is_game_changer = 1" if with_scryfall_flag else ""
    )
    df = pd.read_sql_query(
        f"""SELECT n.name,
                  -- one representative printing: prefer one with art
                  (SELECT c.scryfall_id FROM cards c WHERE c.name = n.name
                   ORDER BY (c.local_image_path IS NULL), (c.image_uri IS NULL), c.scryfall_id
                   LIMIT 1) AS scryfall_id,
                  (SELECT MAX(c.is_game_changer) FROM cards c WHERE c.name = n.name) AS flag_raw,
                  (SELECT GROUP_CONCAT(t.tag) FROM
                       (SELECT DISTINCT tag FROM {tag_table} WHERE card_name = n.name ORDER BY tag) t
                  ) AS tags
           FROM (SELECT card_name AS name FROM {tag_table} {flagged_names}) n
           GROUP BY n.name
           ORDER BY n.name COLLATE NOCASE""",
        conn,
    )
    cards = pd.read_sql_query(
        "SELECT scryfall_id, image_uri, local_image_path FROM cards", conn
    )
    df = df.merge(cards, on="scryfall_id", how="left")
    owned = pd.read_sql_query(
        """SELECT c.name AS name, SUM(col.quantity) AS owned_qty
           FROM collection col JOIN cards c ON c.scryfall_id = col.scryfall_id
           GROUP BY c.name""",
        conn,
    )
    assigned = pd.read_sql_query(
        """SELECT c.name AS name, COUNT(DISTINCT dc.deck_id) AS deck_count
           FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
           GROUP BY c.name""",
        conn,
    )
    df = df.merge(owned, on="name", how="left").merge(assigned, on="name", how="left")
    df["owned_qty"] = df["owned_qty"].fillna(0).astype(int)
    df["deck_count"] = df["deck_count"].fillna(0).astype(int)
    df["has_card"] = df["scryfall_id"].notna()
    df["tags"] = df["tags"].fillna("")
    tagged = df["tags"] != ""
    if with_scryfall_flag:
        df["scryfall_flag"] = df["flag_raw"].fillna(0).astype(int)
        df["scryfall_status"] = [
            "unknown (no card data)" if not has else ("flagged" if flag else "not flagged")
            for has, flag in zip(df["has_card"], df["scryfall_flag"])
        ]
        # Maintenance signal: no card data, tagged but Scryfall disagrees, or
        # Scryfall flags it and nothing has categorised it yet.
        df["needs_attention"] = (
            ~df["has_card"] | (tagged & (df["scryfall_flag"] == 0)) | (~tagged & (df["scryfall_flag"] == 1))
        )
    else:
        df["needs_attention"] = ~df["has_card"]
    df["ownership"] = [
        "In a deck" if decks else ("Owned" if owned_qty else "Not owned")
        for decks, owned_qty in zip(df["deck_count"], df["owned_qty"])
    ]
    return df.drop(columns=["flag_raw"])


def game_changers_overview(conn):
    """One row per name in game_changer_tags OR flagged by Scryfall.
    `categories` is the comma-joined custom categories."""
    return _tag_list_overview(conn, "game_changer_tags", True).rename(columns={"tags": "categories"})


def mana_tags_overview(conn):
    """One row per name in mana_tags; `tags` is the comma-joined tags."""
    return _tag_list_overview(conn, "mana_tags", False)


# ------------------------------------------------------------------
# Orphan report (Card Database -> Card Data "Clean up", UI plan Phase 8).
# Read only: nothing here deletes. Every column that can point at a `cards`
# row is listed once, so "unreferenced" means no reference of ANY kind (a
# delete would otherwise fail a foreign key or lose history).
# ------------------------------------------------------------------
CARD_REFERENCES = [
    ("collection", "scryfall_id"),
    ("deck_cards", "scryfall_id"),
    ("maybeboard", "scryfall_id"),
    ("maybeboard", "replace_scryfall_id"),
    ("deck_swap_queue", "remove_scryfall_id"),
    ("deck_changes", "add_scryfall_id"),
    ("deck_changes", "remove_scryfall_id"),
    ("card_tags", "scryfall_id"),
]


def unreferenced_card_sql(conn):
    """WHERE-clause fragment (against alias `c`) true for a card nothing
    refers to: no lot, deck, shortlist/history row, tag, or tag-list name."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    parts = [
        f"c.scryfall_id NOT IN (SELECT {col} FROM {tbl} WHERE {col} IS NOT NULL)"
        for tbl, col in CARD_REFERENCES if tbl in tables
    ]
    parts.append("c.name NOT IN (SELECT card_name FROM game_changer_tags)")
    parts.append("c.name NOT IN (SELECT card_name FROM mana_tags)")
    return " AND ".join(parts)


def orphan_report(conn, image_dir=None):
    """Stale buckets, each a DataFrame: `unreferenced_cards`,
    `stale_card_tags` (tag rows for a card no longer in that deck),
    `orphan_image_files` (files in the image cache with no `cards` row) and
    `missing_image_files` (cards whose local_image_path file is gone)."""
    unreferenced = pd.read_sql_query(
        f"""SELECT c.scryfall_id, c.name, c.set_code, c.collector_number
            FROM cards c WHERE {unreferenced_card_sql(conn)}
            ORDER BY c.name COLLATE NOCASE""",
        conn,
    )
    stale_tags = pd.read_sql_query(
        """SELECT ct.deck_id, d.name AS deck, ct.scryfall_id, c.name AS card, t.label AS tag
           FROM card_tags ct
           JOIN tags t ON t.tag_id = ct.tag_id
           LEFT JOIN decks d ON d.deck_id = ct.deck_id
           LEFT JOIN cards c ON c.scryfall_id = ct.scryfall_id
           WHERE NOT EXISTS (SELECT 1 FROM deck_cards dc
                             WHERE dc.deck_id = ct.deck_id AND dc.scryfall_id = ct.scryfall_id)
           ORDER BY d.name, c.name""",
        conn,
    )

    if image_dir is None:
        from . import formatting
        image_dir = os.path.join(formatting.STATIC_DIR, "image_cache")
    known_ids = {r[0] for r in conn.execute("SELECT scryfall_id FROM cards")}
    orphan_files = []
    if os.path.isdir(image_dir):
        for filename in sorted(os.listdir(image_dir)):
            # deck_covers/ (user uploads) is a folder in here; skip folders.
            if os.path.isfile(os.path.join(image_dir, filename)) \
                    and os.path.splitext(filename)[0] not in known_ids:
                orphan_files.append(filename)
    static_dir = os.path.dirname(image_dir)  # local_image_path is relative to static/
    missing = [
        (sid, name, path)
        for sid, name, path in conn.execute(
            "SELECT scryfall_id, name, local_image_path FROM cards "
            "WHERE local_image_path IS NOT NULL AND local_image_path != ''"
        )
        if not os.path.isfile(os.path.join(static_dir, path))
    ]
    return {
        "unreferenced_cards": unreferenced,
        "stale_card_tags": stale_tags,
        "orphan_image_files": pd.DataFrame({"file": orphan_files}),
        "missing_image_files": pd.DataFrame(missing, columns=["scryfall_id", "name", "local_image_path"]),
    }


# ------------------------------------------------------------------
# Commander Game Tracking (Phase 2)
# ------------------------------------------------------------------
def games_list(conn):
    """One row per logged game with its participants folded into a
    single display-friendly string, newest first, for the Game History
    view (an actual rendered list, not a raw dump of games/
    game_participants)."""
    games = pd.read_sql_query(
        "SELECT game_id, date, note FROM games ORDER BY date DESC, game_id DESC", conn
    )
    if games.empty:
        return games

    parts = pd.read_sql_query(
        """SELECT gp.game_id, gp.seat,
                  COALESCE(d.name, gp.deck_name) AS deck_name,
                  gp.is_winner, gp.player_name
           FROM game_participants gp
           LEFT JOIN decks d ON d.deck_id = gp.deck_id
           ORDER BY gp.game_id, gp.seat""",
        conn,
    )
    by_game = {gid: grp for gid, grp in parts.groupby("game_id")}

    def render(gid):
        rows = by_game.get(gid)
        if rows is None:
            return []
        out = []
        for _, r in rows.iterrows():
            label = r["deck_name"]
            if pd.notna(r.get("player_name")) and r["player_name"]:
                label += f" ({r['player_name']})"
            if r["is_winner"]:
                label += " 🏆"
            out.append(label)
        return out

    games["participants"] = games["game_id"].apply(render)
    return games


def game_detail(conn, game_id):
    """Full detail for ONE logged game (Prompt Pass 7) — the data
    source for the Commander Game Tracking page's "Edit a logged game"
    form. Returns None if the game doesn't exist, else a dict:
    {"game_id", "date", "note", "participants": [{"seat", "deck_name",
    "deck_id", "is_winner", "player_name"}, ...]} ordered by seat.
    Deliberately not wrapped by a loaders.py @st.cache_data function —
    it's a single-row, on-demand lookup driving a form's defaults, and
    should always reflect the current DB state rather than a cached
    one."""
    row = conn.execute(
        "SELECT game_id, date, note FROM games WHERE game_id=?", (game_id,)
    ).fetchone()
    if not row:
        return None
    gid, date, note = row
    participants = [
        {
            "seat": seat, "deck_name": deck_name, "deck_id": deck_id,
            "is_winner": bool(is_winner), "player_name": player_name,
        }
        for seat, deck_name, deck_id, is_winner, player_name in conn.execute(
            """SELECT seat, deck_name, deck_id, is_winner, player_name
               FROM game_participants WHERE game_id=? ORDER BY seat""",
            (game_id,),
        ).fetchall()
    ]
    return {"game_id": gid, "date": date, "note": note, "participants": participants}


def distinct_player_names(conn):
    """Every distinct player_name ever logged (Prompt Pass 7) — feeds
    the "Player" autofill dropdown on the Commander Game Tracking
    page's log/edit forms, so a regular playgroup doesn't have to be
    retyped by hand each time."""
    rows = conn.execute(
        """SELECT DISTINCT player_name FROM game_participants
           WHERE player_name IS NOT NULL AND TRIM(player_name) != ''
           ORDER BY player_name COLLATE NOCASE"""
    ).fetchall()
    return [r[0] for r in rows]


def distinct_untracked_deck_names(conn):
    """Every distinct deck_name ever logged WITHOUT a tracked deck_id
    (Prompt Pass 7) — an opponent's deck that was never in your own
    `decks` table, OR a deck that WAS tracked but has since been
    deleted (deleting a deck detaches its game_participants rows to
    deck_id=NULL rather than losing them — see writes.delete_deck()).
    Feeds the "known opponent deck" autofill dropdown."""
    rows = conn.execute(
        """SELECT DISTINCT deck_name FROM game_participants
           WHERE deck_id IS NULL
           ORDER BY deck_name COLLATE NOCASE"""
    ).fetchall()
    return [r[0] for r in rows]


def deck_win_rates(conn):
    """All decks' win/loss/win-rate (deck_stats view), decks with at
    least one logged game only, most-played first."""
    return pd.read_sql_query(
        """SELECT name, games_played, wins, losses, win_rate
           FROM deck_stats WHERE games_played > 0
           ORDER BY games_played DESC, win_rate DESC""",
        conn,
    )


def player_win_rates(conn):
    """Win/loss/win-rate by player_name (player_stats view) — only
    reflects games logged since Phase 2 added player_name; older rows
    won't contribute here."""
    return pd.read_sql_query(
        """SELECT player_name, games_played, wins, losses, win_rate
           FROM player_stats
           ORDER BY games_played DESC, win_rate DESC""",
        conn,
    )


def player_elo_ratings(conn, k_factor=20, initial_rating=1000):
    """Player-vs-player ELO variant for the Commander Game Tracking
    Stats tab (Prompt Pass 7's "Win Rates & ELO Tracking" requirement).

    Standard 1v1 ELO assumes a 50% baseline win probability, which
    doesn't fit a 4-player free-for-all pod — an average player there
    wins about 1 game in 4, not 1 in 2. This uses the common
    multiplayer-ELO extension of decomposing each game into every PAIR
    of seated players and running an ordinary pairwise ELO update for
    each pair: the winner is scored a win (1-0) against every other
    seated player, and every pair of NON-winners is scored a draw
    (0.5-0.5) against each other, since a plain win/loss log doesn't
    capture full 2nd/3rd/4th placement. Over many games this settles
    ratings around a baseline appropriate to a 4-player pod rather than
    assuming a 50/50 world — the "Commander Context Adjustments" this
    project's spec asked for. A moderate K-factor (20, vs. chess's
    classic ~32) is used since this is a low-volume, small, consistent
    playgroup (per PROJECT_STATE.md: primarily the dashboard's owner
    plus a handful of regulars) where a single game shouldn't swing a
    rating too wildly.

    Only participant rows with a non-blank player_name count (same
    scoping as player_stats/player_win_rates — games logged before
    Phase 2 added player_name are excluded), and only games with at
    least 2 named players contribute an update (nothing to compare a
    solo-named game against). Games are processed in chronological
    order (date, then game_id as the same-day tiebreaker) so ratings
    evolve the way they actually would have, game by game. Within one
    game, every pairwise delta is computed from a SNAPSHOT of ratings
    taken at the start of that game and applied all at once at the
    end — not applied one pair at a time as they're computed — so a
    player's earlier pairing within the same game can't shift the
    expected-score baseline used for their later pairings in that same
    game. Without this, a 4-player game's outcome would depend on the
    arbitrary order the pairs happen to be iterated in (an artifact
    caught by this pass's own test suite), which isn't a property a
    rating system should have. Never stored — recomputed fresh every
    call, same "can't go stale" philosophy as deck_stats/player_stats.

    Returns a DataFrame [player_name, rating, games_played] — rating
    rounded to the nearest whole number, sorted highest first. Empty
    DataFrame (same columns) if no game has 2+ named players yet."""
    rows = conn.execute(
        """SELECT gp.game_id, g.date, gp.player_name, gp.is_winner
           FROM game_participants gp
           JOIN games g ON g.game_id = gp.game_id
           WHERE gp.player_name IS NOT NULL AND TRIM(gp.player_name) != ''
           ORDER BY g.date, gp.game_id, gp.seat"""
    ).fetchall()

    by_game = {}
    for game_id, date, player_name, is_winner in rows:
        by_game.setdefault((date, game_id), []).append((player_name, bool(is_winner)))

    ratings = {}
    games_played = {}

    def rating_of(name):
        return ratings.setdefault(name, float(initial_rating))

    for participants in by_game.values():
        if len(participants) < 2:
            continue
        for name, _ in participants:
            games_played[name] = games_played.get(name, 0) + 1

        snapshot = {name: rating_of(name) for name, _ in participants}
        deltas = {name: 0.0 for name, _ in participants}
        for (name_a, win_a), (name_b, win_b) in itertools.combinations(participants, 2):
            ra, rb = snapshot[name_a], snapshot[name_b]
            expected_a = 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))
            if win_a and not win_b:
                actual_a = 1.0
            elif win_b and not win_a:
                actual_a = 0.0
            else:
                actual_a = 0.5
            delta_a = k_factor * (actual_a - expected_a)
            deltas[name_a] += delta_a
            deltas[name_b] -= delta_a
        for name, delta in deltas.items():
            ratings[name] = snapshot[name] + delta

    if not ratings:
        return pd.DataFrame(columns=["player_name", "rating", "games_played"])

    out = pd.DataFrame(
        [
            {"player_name": name, "rating": round(rating), "games_played": games_played.get(name, 0)}
            for name, rating in ratings.items()
        ]
    )
    return out.sort_values("rating", ascending=False).reset_index(drop=True)


def player_head_to_head_matrix(conn):
    """Pairwise head-to-head win rate between every two players who've
    ever shared a game (Commander Game Tracking Stats tab, Prompt Pass
    7) — a more literal "how do I do against Anthony specifically"
    complement to player_elo_ratings(). For each shared game between
    two players A and B: if A won and B didn't (or vice versa), that's
    a win/loss for the pair; if neither did (including a historical
    pre-Prompt-Pass-7 logged draw), it still counts toward games shared
    but doesn't move either player's numerator — same "only a recorded
    winner moves the needle" logic as the rest of this page.

    Returns a square DataFrame indexed and columned by player name
    (alphabetical): cell [A, B] is A's win rate against B specifically
    (float 0-1, NaN if they've never shared a game); the diagonal is
    always NaN. Same player_name scoping as player_elo_ratings() —
    only games with a recorded player_name contribute. Never stored,
    recomputed fresh every call."""
    rows = conn.execute(
        """SELECT gp.game_id, gp.player_name, gp.is_winner
           FROM game_participants gp
           WHERE gp.player_name IS NOT NULL AND TRIM(gp.player_name) != ''"""
    ).fetchall()

    by_game = {}
    for game_id, player_name, is_winner in rows:
        by_game.setdefault(game_id, []).append((player_name, bool(is_winner)))

    wins = {}       # (a, b) -> a's win count vs b
    together = {}   # (a, b) -> games shared, stored symmetrically both directions
    players = set()

    for participants in by_game.values():
        if len(participants) < 2:
            continue
        for name, _ in participants:
            players.add(name)
        for (name_a, win_a), (name_b, win_b) in itertools.combinations(participants, 2):
            together[(name_a, name_b)] = together.get((name_a, name_b), 0) + 1
            together[(name_b, name_a)] = together.get((name_b, name_a), 0) + 1
            if win_a and not win_b:
                wins[(name_a, name_b)] = wins.get((name_a, name_b), 0) + 1
            elif win_b and not win_a:
                wins[(name_b, name_a)] = wins.get((name_b, name_a), 0) + 1

    names = sorted(players)
    if not names:
        return pd.DataFrame()

    data = {}
    for col in names:
        col_vals = []
        for row_name in names:
            if row_name == col:
                col_vals.append(float("nan"))
                continue
            shared = together.get((row_name, col), 0)
            col_vals.append(round(wins.get((row_name, col), 0) / shared, 3) if shared else float("nan"))
        data[col] = col_vals
    return pd.DataFrame(data, index=names)


# ------------------------------------------------------------------
# Home-page summary
# ------------------------------------------------------------------
def dashboard_summary(conn):
    def one(q):
        return conn.execute(q).fetchone()[0]

    return {
        "total_decks": one("SELECT COUNT(*) FROM decks"),
        "unique_printings": one("SELECT COUNT(*) FROM cards"),
        "collection_lots": one("SELECT COUNT(*) FROM collection"),
        "games_logged": one("SELECT COUNT(*) FROM games"),
        "idea_rows": one("SELECT COUNT(*) FROM deck_changes WHERE status = 'idea'"),
        "planned_rows": one("SELECT COUNT(*) FROM deck_changes WHERE status = 'planned'"),
    }

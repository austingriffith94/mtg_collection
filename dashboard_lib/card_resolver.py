"""
Card resolution for "add a new card" flows in the Editor page: given a
name (and optionally an exact set + collector number), find or create the
matching `cards` row.

Checks the local `cards` table first — most adds are cards already known
from another deck/lot in your database — and only falls back to a live
Scryfall lookup (reusing scripts/scryfall_lookup.py's client) when nothing
local matches. That live fallback needs the `requests` package and actual
network access; neither is required just to browse/edit data already in
your database, only to add a genuinely new card the dashboard hasn't seen
before.
"""
import datetime
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")


def _load_scryfall_module():
    if SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, SCRIPTS_DIR)
    import scryfall_lookup  # requires `requests` — caller should catch ImportError
    return scryfall_lookup


def scryfall_available():
    try:
        _load_scryfall_module()
        return True
    except ImportError:
        return False


def find_local_card(conn, name=None, set_code=None, collector_number=None):
    """Look for a printing already present in the local `cards` table
    without touching the network. Returns a scryfall_id or None. An exact
    set+number match is preferred (unambiguous); a bare name match just
    returns whichever printing happens to be in the DB first."""
    if set_code and collector_number:
        row = conn.execute(
            "SELECT scryfall_id FROM cards WHERE LOWER(set_code)=LOWER(?) AND collector_number=?",
            (set_code.strip(), str(collector_number).strip()),
        ).fetchone()
        if row:
            return row[0]
    if name:
        row = conn.execute(
            "SELECT scryfall_id FROM cards WHERE LOWER(name)=LOWER(?) LIMIT 1",
            (name.strip(),),
        ).fetchone()
        if row:
            return row[0]
    return None


def _insert_card_row(conn, row):
    """Shared INSERT OR IGNORE for a normalized scryfall_lookup.to_card_row()
    dict — used by both resolve_or_fetch_card() (a single new printing) and
    fetch_additional_printings() (Prompt Pass 6, potentially several at
    once), so the column list only lives in one place. Does NOT commit —
    callers batch their own commit()."""
    conn.execute(
        """INSERT OR IGNORE INTO cards (
            scryfall_id, oracle_id, name, set_code, collector_number, type_line,
            mana_cost, cmc, color_identity, oracle_text, rarity, image_uri,
            is_basic_land, is_game_changer, is_reserved, is_showcase, is_borderless,
            commander_legal, current_price_usd,
            price_updated_at, last_fetched_at
        ) VALUES (:scryfall_id, :oracle_id, :name, :set_code, :collector_number, :type_line,
                  :mana_cost, :cmc, :color_identity, :oracle_text, :rarity, :image_uri,
                  :is_basic_land, :is_game_changer, :is_reserved, :is_showcase, :is_borderless,
                  :commander_legal, :current_price_usd,
                  :price_updated_at, :last_fetched_at)""",
        row,
    )


def resolve_or_fetch_card(conn, name=None, set_code=None, collector_number=None):
    """Returns (scryfall_id, was_newly_fetched, error). error is None on
    success. Checks locally first (no network needed); falls back to a
    live Scryfall lookup only if nothing local matches."""
    name = (name or "").strip() or None
    set_code = (set_code or "").strip() or None
    collector_number = (str(collector_number).strip() if collector_number not in (None, "") else None)

    if not name and not (set_code and collector_number):
        return None, False, "Enter a card name, or a set code + collector number."

    local = find_local_card(conn, name=name, set_code=set_code, collector_number=collector_number)
    if local:
        return local, False, None

    try:
        scryfall_lookup = _load_scryfall_module()
    except ImportError:
        return None, False, (
            "Not found in your local database, and the `requests` package isn't "
            "installed, so a live Scryfall lookup isn't possible. Run "
            "`pip install requests` to enable adding brand-new cards, or add this "
            "one via the CSV migration instead."
        )

    client = scryfall_lookup.ScryfallClient(verbose=False)
    if set_code and collector_number:
        data = client.get_by_set_number(set_code, collector_number)
    else:
        data = client.get_by_name(name)

    if not data:
        target = f"{set_code}/{collector_number}" if set_code and collector_number else name
        return None, False, (
            f"Couldn't resolve '{target}' on Scryfall — check the spelling/set code, "
            f"or that you have network access right now."
        )

    row = scryfall_lookup.to_card_row(data)
    today = datetime.date.today().isoformat()
    row["price_updated_at"] = today
    row["last_fetched_at"] = today

    _insert_card_row(conn, row)
    conn.commit()
    return row["scryfall_id"], True, None


def fetch_additional_printings(conn, name):
    """Prompt Pass 6 — powers the Collection tab's "look up more
    printings" button: queries Scryfall for EVERY printing of `name`
    (via ScryfallClient.get_all_printings(), unique=prints) and inserts
    any not already in the local `cards` table (existing rows are left
    untouched — INSERT OR IGNORE via _insert_card_row()). This is the one
    place in the dashboard that deliberately fetches more than "whatever's
    actually referenced" (scryfall_lookup.py's module docstring) — it's an
    explicit, user-initiated action for a card the user already confirmed
    they own/want, not a background bulk sync.

    Returns (added_count, error) — error is a message on failure (missing
    `requests`, or nothing found on Scryfall for this name), else None.
    added_count is 0 (not an error) if Scryfall has the card but every
    printing it returned was already known locally."""
    name = (name or "").strip()
    if not name:
        return 0, "Enter a card name first."

    try:
        scryfall_lookup = _load_scryfall_module()
    except ImportError:
        return 0, (
            "The `requests` package isn't installed, so a live Scryfall lookup "
            "isn't possible. Run `pip install requests` to enable this."
        )

    client = scryfall_lookup.ScryfallClient(verbose=False)
    results = client.get_all_printings(name)
    if not results:
        return 0, f"No printings found on Scryfall for '{name}'."

    today = datetime.date.today().isoformat()
    added = 0
    for data in results:
        row = scryfall_lookup.to_card_row(data)
        if not row:
            continue
        already_known = conn.execute(
            "SELECT 1 FROM cards WHERE scryfall_id=?", (row["scryfall_id"],)
        ).fetchone()
        if already_known:
            continue
        row["price_updated_at"] = today
        row["last_fetched_at"] = today
        _insert_card_row(conn, row)
        added += 1
    conn.commit()
    return added, None

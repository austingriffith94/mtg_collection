"""
Refreshes Scryfall-derived fields for every card already in your
database — Reserved List flag, Game Changer flag, Commander legality,
and current price — WITHOUT the full wipe-and-rebuild migrate.py does.

Fetches each card directly by its permanent scryfall_id (Scryfall's
fastest, most precise lookup — no re-resolving by name or set+number
needed), so this stays correct even as WotC/Scryfall update the Game
Changers or Reserved List over time, or as prices/legality change.

As of Phase 1, this also automatically prunes the collection afterward
(dashboard_lib.writes.prune_collection): any collection lot with no
location, a quantity of 0/none, and not present in any deck's mainboard
or maybeboard is deleted. This is the only thing here that touches your
collection rather than just `cards` — everything else (decklists, tags)
is untouched.

Needs `requests` + network access (reuses scripts/scryfall_lookup.py, the
same client migrate.py and card_resolver.py use). Shared by
scripts/refresh_card_data.py (CLI) and the Editor page's "Card Data" tab.
"""
import datetime
import os
import sys

from . import writes as w

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")


def _load_scryfall_module():
    if SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, SCRIPTS_DIR)
    import scryfall_lookup  # requires `requests` — caller should catch ImportError
    return scryfall_lookup


def refresh_all_cards(conn, progress_callback=None):
    """Re-fetches every row in `cards` by scryfall_id and updates
    is_reserved / is_game_changer / commander_legal / current_price_usd
    in place. progress_callback(done, total, card_name), if given, is
    called after each card so callers can show progress. Returns a
    summary dict: checked, updated, newly_reserved, newly_game_changer,
    unresolved, pruned_cards (all lists of card names), pruned_count."""
    scryfall_lookup = _load_scryfall_module()
    client = scryfall_lookup.ScryfallClient(verbose=False)

    rows = conn.execute("SELECT scryfall_id, name, is_reserved, is_game_changer FROM cards ORDER BY name").fetchall()
    total = len(rows)
    today = datetime.date.today().isoformat()

    summary = {
        "checked": 0,
        "updated": 0,
        "newly_reserved": [],
        "newly_game_changer": [],
        "unresolved": [],
        "pruned_count": 0,
        "pruned_cards": [],
    }

    for i, (scryfall_id, name, was_reserved, was_game_changer) in enumerate(rows, start=1):
        data = client.get_by_id(scryfall_id)
        summary["checked"] += 1

        if not data:
            summary["unresolved"].append(name)
        else:
            fresh = scryfall_lookup.to_card_row(data)
            conn.execute(
                """UPDATE cards SET is_reserved=?, is_game_changer=?, is_showcase=?,
                       is_borderless=?, commander_legal=?,
                       current_price_usd=?, price_updated_at=?, last_fetched_at=?
                   WHERE scryfall_id=?""",
                (fresh["is_reserved"], fresh["is_game_changer"], fresh["is_showcase"],
                 fresh["is_borderless"], fresh["commander_legal"],
                 fresh["current_price_usd"], today, today, scryfall_id),
            )
            summary["updated"] += 1
            if fresh["is_reserved"] and not was_reserved:
                summary["newly_reserved"].append(name)
            if fresh["is_game_changer"] and not was_game_changer:
                summary["newly_game_changer"].append(name)

        if progress_callback:
            progress_callback(i, total, name)

    conn.commit()

    pruned = w.prune_collection(conn)
    summary["pruned_count"] = len(pruned)
    summary["pruned_cards"] = [name for _, name in pruned]

    return summary

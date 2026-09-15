"""
Refresh Scryfall-derived fields (Reserved List, Game Changer, Commander
legality, price) for every card already in your database — WITHOUT
wiping or rebuilding anything else, unlike migrate.py. Updates
cards.is_reserved / is_game_changer / commander_legal / current_price_usd
/ price_updated_at / last_fetched_at, matched by each card's permanent
scryfall_id. Doesn't touch your decklists or tags.

It DOES also prune your collection afterward: any collection lot with no
assigned location, a quantity of 0/none, and not present in any deck's
mainboard or maybeboard is deleted (see dashboard_lib.writes.prune_collection).

Safe to run anytime, as often as you like — this is a normal maintenance
task, unlike migrate.py, which is one-way and meant for your initial CSV
import only (see migrate.py's module docstring).

This is also available directly in the dashboard (Editor page → "Card
Data" tab), sharing this exact logic via dashboard_lib/refresh.py so the
two never drift apart.

Usage:
    python refresh_card_data.py
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "mtg_collection.db")

sys.path.insert(0, BASE_DIR)
from dashboard_lib import refresh as refresh_lib
from dashboard_lib import queries as q


def main():
    if not os.path.exists(DB_PATH):
        print(f"No database found at {DB_PATH} — run migrate.py first.")
        sys.exit(1)

    conn = q.get_connection(DB_PATH)  # also applies any pending schema upgrades

    total = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    print(f"Refreshing Scryfall data for {total} card(s) in your database...")

    def progress(done, total, name):
        if done % 25 == 0 or done == total:
            print(f"  {done}/{total}")

    summary = refresh_lib.refresh_all_cards(conn, progress_callback=progress)

    print()
    print("=" * 60)
    print(f"Checked: {summary['checked']}")
    print(f"Updated: {summary['updated']}")
    if summary["newly_reserved"]:
        print(f"\nNewly flagged Reserved List ({len(summary['newly_reserved'])}):")
        for n in summary["newly_reserved"]:
            print(f"  - {n}")
    if summary["newly_game_changer"]:
        print(f"\nNewly flagged Game Changer ({len(summary['newly_game_changer'])}):")
        for n in summary["newly_game_changer"]:
            print(f"  - {n}")
    if summary["unresolved"]:
        print(f"\n⚠ {len(summary['unresolved'])} card(s) couldn't be refreshed:")
        for n in summary["unresolved"]:
            print(f"  - {n}")

    print(f"\nPruned {summary['pruned_count']} collection lot(s) with no location, "
          f"no quantity, and not in any deck's mainboard/maybeboard.")
    if summary["pruned_cards"]:
        for n in summary["pruned_cards"]:
            print(f"  - {n}")
    print("=" * 60)


if __name__ == "__main__":
    main()

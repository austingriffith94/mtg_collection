"""
Offline dry-run of migrate.py using a MOCKED Scryfall client, so the
surrounding logic (CSV parsing, date math, resolution, schema inserts)
can be validated without live network access. Card metadata here is
synthetic/deterministic — NOT real Scryfall data. This file is a dev
aid only; it is not part of the delivered package.
"""
import os
import sys
import uuid
import hashlib

sys.path.insert(0, os.path.dirname(__file__))
import scryfall_lookup
import migrate

BASIC_LANDS = {"plains", "island", "swamp", "mountain", "forest", "wastes"}


def fake_id(*parts):
    h = hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()
    return str(uuid.UUID(h))


class MockScryfallClient(scryfall_lookup.ScryfallClient):
    """Synthesizes plausible-shaped Scryfall JSON from whatever we already
    know (name / set / number), so migrate.py's control flow can be tested
    without hitting the real API."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls_by_setnum = 0
        self.calls_by_name = 0

    def get_by_set_number(self, set_code, collector_number):
        self.calls_by_setnum += 1
        name = f"TestCard {set_code}-{collector_number}"
        return self._synth(name, set_code, collector_number)

    def get_by_name(self, name):
        self.calls_by_name += 1
        # simulate the one genuinely-unresolvable maybeboard card
        if name.strip().lower() == "cantankerous keepers":
            return None
        set_code = "tst"
        collector_number = str(abs(hash(name)) % 999)
        return self._synth(name, set_code, collector_number)

    def _synth(self, name, set_code, collector_number):
        is_basic = name.strip().lower() in BASIC_LANDS
        # simulate Scryfall's live game_changer flag agreeing with a couple
        # of known names, and DISAGREEING on one, to exercise drift reporting
        game_changer = name in ("Jeska's Will", "Bolas's Citadel", "Sol Ring")
        return {
            "id": fake_id(name, set_code, collector_number),
            "oracle_id": fake_id(name),
            "name": name,
            "set": str(set_code).lower(),
            "collector_number": str(collector_number),
            "type_line": "Basic Land" if is_basic else "Creature — Test",
            "mana_cost": "" if is_basic else "{1}{B}",
            "cmc": 0 if is_basic else 2,
            "color_identity": [] if is_basic else ["B"],
            "oracle_text": "" if is_basic else "Test oracle text.",
            "rarity": "common",
            "image_uris": {"normal": f"https://example.com/{fake_id(name)}.jpg"},
            "legalities": {"commander": "legal"},
            "prices": {"usd": "1.23"},
            "game_changer": game_changer,
        }


def main():
    # monkeypatch so migrate.py's Migrator uses the mock client
    migrate.ScryfallClient = MockScryfallClient

    test_db = os.path.join(migrate.BASE_DIR, "_test_mtg_collection.db")
    m = migrate.Migrator(db_path=test_db, verbose=True)
    m.run()

    # --- sanity checks ---
    import sqlite3
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()

    def one(q, *a):
        return cur.execute(q, a).fetchone()[0]

    print("\n" + "=" * 60)
    print("SANITY CHECKS")
    print("=" * 60)
    print("decks:", one("SELECT COUNT(*) FROM decks"))
    print("  active:", one("SELECT COUNT(*) FROM decks WHERE is_active=1"))
    print("  retired:", one("SELECT COUNT(*) FROM decks WHERE is_active=0"))
    print("cards:", one("SELECT COUNT(*) FROM cards"))
    print("collection rows:", one("SELECT COUNT(*) FROM collection"))
    print("deck_cards rows:", one("SELECT COUNT(*) FROM deck_cards"))
    print("maybeboard rows:", one("SELECT COUNT(*) FROM maybeboard"))
    print("tags:", one("SELECT COUNT(*) FROM tags"))
    print("card_tags rows:", one("SELECT COUNT(*) FROM card_tags"))
    print("games:", one("SELECT COUNT(*) FROM games"))
    print("game_participants:", one("SELECT COUNT(*) FROM game_participants"))
    print("deck_win_conditions:", one("SELECT COUNT(*) FROM deck_win_conditions"))
    print("deck_strengths:", one("SELECT COUNT(*) FROM deck_strengths"))
    print("deck_weaknesses:", one("SELECT COUNT(*) FROM deck_weaknesses"))
    print("deck_themes:", one("SELECT COUNT(*) FROM deck_themes"))

    # Raktres should have exactly 100 cards (per user confirmation)
    raktres_total = one(
        "SELECT SUM(quantity) FROM deck_cards dc JOIN decks d ON d.deck_id=dc.deck_id "
        "WHERE d.name = 'Raktres, Lord of Discounts'"
    )
    print(f"\nRaktres total card count (expect 100): {raktres_total}")
    assert raktres_total == 100, "Raktres card count mismatch!"

    # retired deck linkage
    row = cur.execute(
        "SELECT r.name, r.is_active, s.name FROM decks r "
        "JOIN decks s ON s.deck_id = r.successor_deck_id "
        "WHERE r.name = 'Rakdos Showstopper'"
    ).fetchone()
    print(f"Retired deck link: {row} (expect Rakdos Showstopper -> Raktres, is_active=0)")
    assert row == ("Rakdos Showstopper", 0, "Raktres, Lord of Discounts")

    # win/loss view sanity
    print("\ndeck_stats view sample:")
    for r in cur.execute(
        "SELECT name, games_played, wins, losses, win_rate FROM deck_stats "
        "WHERE games_played > 0 ORDER BY games_played DESC LIMIT 5"
    ):
        print(" ", r)

    # tags sanity — strategy tag counts should roughly match PDF strategy tables
    print("\nRaktres strategy tag breakdown:")
    for r in cur.execute(
        """SELECT t.label, COUNT(*), ROUND(AVG(c.cmc),2)
           FROM card_tags ct
           JOIN tags t ON t.tag_id = ct.tag_id
           JOIN decks d ON d.deck_id = ct.deck_id
           JOIN cards c ON c.scryfall_id = ct.scryfall_id
           WHERE d.name = 'Raktres, Lord of Discounts'
           GROUP BY t.label ORDER BY COUNT(*) DESC"""
    ):
        print(" ", r)

    # win condition sanity
    print("\nRaktres win conditions:")
    for r in cur.execute(
        "SELECT dwc.rank, dwc.label, dwc.description FROM deck_win_conditions dwc "
        "JOIN decks d ON d.deck_id=dwc.deck_id WHERE d.name='Raktres, Lord of Discounts' ORDER BY dwc.rank"
    ):
        print(" ", r)

    # game participants sanity: unmatched (opponent) decks should have deck_id NULL but still stored
    print("\nSample game_participants (own vs opponent):")
    for r in cur.execute(
        "SELECT deck_name, deck_id, is_own_deck, is_winner FROM game_participants LIMIT 8"
    ):
        print(" ", r)

    print("\ngame_changer_tags:", one("SELECT COUNT(*) FROM game_changer_tags"))
    print("mana_tags:", one("SELECT COUNT(*) FROM mana_tags"))

    print("\ndeck_mana_tag_summary sample (Vilis, Blood ATM):")
    for r in cur.execute(
        """SELECT dmts.tag, dmts.cards FROM deck_mana_tag_summary dmts
           JOIN decks d ON d.deck_id = dmts.deck_id
           WHERE d.name = 'Vilis, Blood ATM'"""
    ):
        print(" ", r)

    print("\ndeck_game_changers sample (all decks):")
    for r in cur.execute(
        """SELECT d.name, dgc.card_name, dgc.scryfall_flag, dgc.custom_tag
           FROM deck_game_changers dgc JOIN decks d ON d.deck_id = dgc.deck_id"""
    ):
        print(" ", r)

    conn.close()
    print("\nAll sanity checks passed." )


if __name__ == "__main__":
    main()

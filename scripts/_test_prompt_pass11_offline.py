"""
Offline test for Prompt Pass 11 changes: no Streamlit, no live network.

Prompt Pass 11 ("prompt4.txt" — Commander Cast Probability land-drop fix +
Sleeved Card Count fix):

  1. dashboard_lib/probability.py — commander_cast_probability_by_turn()
     and card_cast_probability_by_turn() used to compute their land_factor
     purely from "at least N lands DRAWN by this turn," ignoring the
     standard one-land-drop-per-turn rule. With no ramp/fast mana
     accounted for, turn T can put at most T lands into play no matter
     how many extra lands got drawn, so a land-flooded draw could show a
     nonzero chance of casting a 4-mana-value commander on turn 3, which
     isn't legal. Both functions now report 0% whenever turn < the
     required land count.
  2. dashboard_lib/queries.py — in_deck_sleeved_count() used to only sum
     collection rows whose Location matches the deck's name, which always
     undercounts a deck by however many basic lands it runs (basics never
     get their own collection row per this project's tracking convention
     — see schema.sql's own comment on collection.quantity). It now also
     adds the deck list's own cards.is_basic_land=1 quantities, and takes
     a deck_id parameter to do so. loaders.load_in_deck_sleeved_count()
     and pages/2_Decks.py's call site were updated to pass it through.

Covers:
  A. commander_cast_probability_by_turn() — 0% below the mana-value turn
     even with a heavily land-flooded deck; nonzero once turn catches up;
     unaffected turns/values still match the plain hypergeometric math.
  B. card_cast_probability_by_turn() — same land-drop floor, including
     the turn=0 (opening hand) edge case for a 0-mana-value card.
  C. queries.in_deck_sleeved_count() against a real scratch schema.sql
     database — basics-only deck, collection-only deck, and a mix of
     both, plus the "nothing at all" None case.
  D. Source-text checks confirming the call-site wiring (loaders.py,
     pages/2_Decks.py) landed, not just that the query function works in
     isolation.

Run from anywhere:
    python scripts/_test_prompt_pass11_offline.py
"""
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dashboard_lib import probability as prob
from dashboard_lib import queries as q


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(f"Test failed: {label}")


def build_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(PROJECT_ROOT, "schema.sql")) as f:
        conn.executescript(f.read())

    conn.execute("INSERT INTO decks (deck_id, name, commander) VALUES (1, 'Basics Only', 'Some Commander')")
    conn.execute("INSERT INTO decks (deck_id, name, commander) VALUES (2, 'Collection Only', 'Some Commander')")
    conn.execute("INSERT INTO decks (deck_id, name, commander) VALUES (3, 'Mixed Deck', 'Some Commander')")
    conn.execute("INSERT INTO decks (deck_id, name, commander) VALUES (4, 'Empty Deck', 'Some Commander')")

    cards = [
        # scryfall_id, name, type_line, is_basic_land
        ("forest-1", "Forest", "Basic Land — Forest", 1),
        ("island-1", "Island", "Basic Land — Island", 1),
        ("sol-ring", "Sol Ring", "Artifact", 0),
        ("goblin-1", "Goblin Chieftain", "Creature — Goblin", 0),
    ]
    for sid, name, type_line, is_basic in cards:
        conn.execute(
            "INSERT INTO cards (scryfall_id, name, set_code, collector_number, type_line, is_basic_land) "
            "VALUES (?,?,'TST','1',?,?)",
            (sid, name, type_line, is_basic),
        )

    # Deck 1: 10 Forests, 5 Islands, no collection rows at all for them
    # (matches the real-world convention: basics are never individually
    # tracked in the collection).
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1, 'forest-1', 10)")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1, 'island-1', 5)")

    # Deck 2: one nonland card, tracked in the collection with a matching
    # Location, no basics at all.
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (2, 'sol-ring', 1)")
    conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, location) VALUES ('sol-ring', 1, 'Collection Only')"
    )

    # Deck 3: a mix — 7 Forests (untracked, per convention) plus a
    # collection-tracked Goblin Chieftain sleeved in this same deck.
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (3, 'forest-1', 7)")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (3, 'goblin-1', 1)")
    conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, location) VALUES ('goblin-1', 1, 'Mixed Deck')"
    )

    # Deck 4: nothing at all — no deck_cards rows, no collection rows.
    conn.commit()
    return conn


def main():
    # ------------------------------------------------------------------
    # A. commander_cast_probability_by_turn() — land-drop floor
    # ------------------------------------------------------------------
    # A heavily land-flooded 40-card library (30 lands) so P(>=4 lands
    # drawn by turn 3) would be large under the old, buggy formula.
    rows = prob.commander_cast_probability_by_turn(
        library_size=40,
        land_count=30,
        source_counts={},
        commander_cmc=4,
        colored_pips={},
        on_the_play=True,
        max_turn=10,
    )
    by_turn = {r["turn"]: r for r in rows}
    check(
        "turn 1 (< 4 mana value) is exactly 0% even with a land-flooded deck",
        by_turn[1]["probability"] == 0.0 and by_turn[1]["land_factor"] == 0.0,
    )
    check("turn 2 (< 4) is exactly 0%", by_turn[2]["land_factor"] == 0.0)
    check("turn 3 (< 4) is exactly 0%", by_turn[3]["land_factor"] == 0.0)
    check(
        "turn 4 (== mana value) is nonzero — the floor only blocks turns BELOW the requirement",
        by_turn[4]["land_factor"] > 0.0,
    )
    # From turn 4 onward the land_factor should match the plain
    # hypergeometric "at least 4 lands seen" math directly (the floor is
    # a no-op once turn >= needed_lands).
    seen_t4 = prob.cards_seen_by_turn(4, True)
    expected_t4 = prob.prob_at_least(40, 30, seen_t4, 4)
    check("turn 4's land_factor matches the plain hypergeometric math exactly", by_turn[4]["land_factor"] == expected_t4)

    # A 0-mana-value commander (needed_lands=0) is never floored — every
    # turn from 1 should already be nonzero (well, 100%, since 0 lands
    # are always "at least drawn").
    zero_cmc_rows = prob.commander_cast_probability_by_turn(
        library_size=40, land_count=30, source_counts={}, commander_cmc=0,
        colored_pips={}, on_the_play=True, max_turn=2,
    )
    check(
        "a 0-mana-value commander is never floored to 0 by this fix",
        all(r["land_factor"] == 1.0 for r in zero_cmc_rows),
    )

    # ------------------------------------------------------------------
    # B. card_cast_probability_by_turn() — same floor, plus turn=0 edge case
    # ------------------------------------------------------------------
    card_rows = prob.card_cast_probability_by_turn(
        library_size=40, land_count=30, source_counts={}, card_qty=1,
        card_cmc=3, colored_pips={}, on_the_play=True, max_turn=5,
    )
    card_by_turn = {r["turn"]: r for r in card_rows}
    check("card: turn 0 (opening hand, < 3 mana value) floored to 0%", card_by_turn[0]["land_factor"] == 0.0)
    check("card: turn 2 (< 3) floored to 0%", card_by_turn[2]["land_factor"] == 0.0)
    check("card: turn 3 (== mana value) is nonzero", card_by_turn[3]["land_factor"] > 0.0)

    # A 0-mana-value card at turn 0 must NOT be floored (0 < 0 is false) —
    # confirms the boundary condition is turn < needed_lands, not <=.
    free_card_rows = prob.card_cast_probability_by_turn(
        library_size=40, land_count=30, source_counts={}, card_qty=1,
        card_cmc=0, colored_pips={}, on_the_play=True, max_turn=1,
    )
    check(
        "a 0-mana-value card's land_factor at turn 0 is 100%, not floored",
        free_card_rows[0]["land_factor"] == 1.0,
    )

    # ------------------------------------------------------------------
    # C. queries.in_deck_sleeved_count() — real DB
    # ------------------------------------------------------------------
    conn = build_db()

    check(
        "basics-only deck: 15 sleeved from deck-list basics alone, zero collection rows",
        q.in_deck_sleeved_count(conn, 1, "Basics Only") == 15,
    )
    check(
        "collection-only deck: 1 sleeved from the tracked collection row, no basics",
        q.in_deck_sleeved_count(conn, 2, "Collection Only") == 1,
    )
    check(
        "mixed deck: 7 untracked basics + 1 tracked nonland = 8 total",
        q.in_deck_sleeved_count(conn, 3, "Mixed Deck") == 8,
    )
    check(
        "a deck with neither basics nor collection rows returns None (not 0)",
        q.in_deck_sleeved_count(conn, 4, "Empty Deck") is None,
    )
    check(
        "a deck_id/name pair that doesn't exist at all also returns None",
        q.in_deck_sleeved_count(conn, 999, "Nonexistent Deck") is None,
    )
    conn.close()

    # ------------------------------------------------------------------
    # D. Source-text checks — call-site wiring
    # ------------------------------------------------------------------
    loaders_path = os.path.join(PROJECT_ROOT, "dashboard_lib", "loaders.py")
    with open(loaders_path, encoding="utf-8") as f:
        loaders_text = f.read()
    check(
        "loaders.load_in_deck_sleeved_count() now takes deck_id and passes it through",
        "def load_in_deck_sleeved_count(_conn, deck_id, deck_name):" in loaders_text
        and "q.in_deck_sleeved_count(_conn, deck_id, deck_name)" in loaders_text,
    )

    page_path = os.path.join(PROJECT_ROOT, "pages", "2_Decks.py")
    with open(page_path, encoding="utf-8") as f:
        page_text = f.read()
    check(
        "pages/2_Decks.py's call site passes deck_id through to the loader",
        "loaders.load_in_deck_sleeved_count(conn, deck_id, meta.get(\"name\"))" in page_text,
    )

    print("\nAll Prompt Pass 11 offline checks passed.")


if __name__ == "__main__":
    main()

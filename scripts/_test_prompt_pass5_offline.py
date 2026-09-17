"""
Offline test for Prompt Pass 5 changes: no Streamlit, no network.

Covers:
  1. probability.count_color_sources() — the lands_only=False bug fix
     (a plain nonland card sharing a color must NOT count as a mana
     source; only lands and verified mana rocks/dorks should).
  2. probability.card_cast_probability_by_turn() — the new per-card
     "Check a specific card" engine (draw x land x color factors).
  3. probability.expected_count_by_turn() / weighted_mana_expectation()
     — the new "expected lands seen" helper and its refactor.
  4. EDHREC Salt Score removal — the column stays in schema.sql
     (additive/non-destructive policy) but is no longer selected,
     written, or exposed anywhere in the app.

Run from anywhere:
    python scripts/_test_prompt_pass5_offline.py
"""
import os
import sqlite3
import sys
import types

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# dashboard_lib.loaders is the one module here with a real `import
# streamlit` at the top (for the @st.cache_data decorators) — streamlit
# itself isn't installed in this offline sandbox (see PROJECT_STATE.md's
# testing caveat), so a minimal stub is registered in sys.modules before
# importing it, same spirit as _test_phase4_offline.py's fake
# session_state, just enough for the module to import cleanly.
if "streamlit" not in sys.modules:
    _fake_streamlit = types.ModuleType("streamlit")

    def _fake_cache_data(*args, **kwargs):
        def _decorator(func):
            func.clear = lambda: None
            return func
        return _decorator

    _fake_streamlit.cache_data = _fake_cache_data
    sys.modules["streamlit"] = _fake_streamlit

from dashboard_lib import probability as prob
from dashboard_lib import queries as q
from dashboard_lib import writes as w
from dashboard_lib import loaders as ld


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(f"Test failed: {label}")


def build_library_df():
    """A tiny hand-built library dataframe (already in the
    card_view.add_derived_columns() shape — is_land present) covering:
      - a basic-ish red land with plain colored text ("Add {R}.")
      - a nonland mana rock with a colored ability ("Add {R}.")
      - an "any color" nonland dork (Birds-of-Paradise-style text)
      - a plain nonland RED creature with NO mana ability at all — this
        is the one the Prompt Pass 5 fix must exclude from color-source
        counts when lands_only=False, since it shares color R but never
        taps for mana.
    """
    rows = [
        {
            "name": "Test Mountain", "quantity": 10, "is_land": True,
            "type_line": "Basic Land — Mountain", "oracle_text": "{T}: Add {R}.",
            "color_identity": "",
        },
        {
            "name": "Test Signet", "quantity": 1, "is_land": False,
            "type_line": "Artifact", "oracle_text": "{T}: Add {R}.",
            "color_identity": "",
        },
        {
            "name": "Test Bird", "quantity": 1, "is_land": False,
            "type_line": "Creature — Bird", "oracle_text": "{T}: Add one mana of any color.",
            "color_identity": "",
        },
        {
            "name": "Test Vanilla Red Creature", "quantity": 1, "is_land": False,
            "type_line": "Creature — Human Warrior", "oracle_text": "First strike.",
            "color_identity": "R",
        },
    ]
    return pd.DataFrame(rows)


def main():
    # ------------------------------------------------------------------
    # 1. count_color_sources — nonland gate fix
    # ------------------------------------------------------------------
    lib = build_library_df()

    lands_only_r = prob.count_color_sources(lib, "R", lands_only=True)
    check("lands_only=True counts only the land (qty 10)", lands_only_r == 10)

    all_sources_r = prob.count_color_sources(lib, "R", lands_only=False)
    check(
        "lands_only=False counts land + signet + any-color bird (10+1+1=12), "
        "NOT the vanilla red creature",
        all_sources_r == 12,
    )

    # Sanity: if the bug were still present, the vanilla creature's
    # color_identity="R" fallback would add its qty=1, giving 13 instead
    # of 12 — assert the fixed (lower) total explicitly.
    check("vanilla nonland creature is excluded (would be 13 if still buggy)", all_sources_r != 13)

    # ------------------------------------------------------------------
    # 2. card_cast_probability_by_turn
    # ------------------------------------------------------------------
    rows = prob.card_cast_probability_by_turn(
        library_size=99,
        land_count=35,
        source_counts={"R": 12},
        card_qty=1,
        card_cmc=3,
        colored_pips={"R": 1},
        on_the_play=True,
        max_turn=8,
    )
    check("returns turn 0 (opening hand) through turn 8 inclusive (9 rows)", len(rows) == 9)
    check("turn field is 0..8 in order", [r["turn"] for r in rows] == list(range(9)))
    for r in rows:
        check(
            f"turn {r['turn']}: all factors in [0,1]",
            0.0 <= r["draw_factor"] <= 1.0 and 0.0 <= r["land_factor"] <= 1.0
            and 0.0 <= r["color_factor"] <= 1.0 and 0.0 <= r["probability"] <= 1.0,
        )
        check(
            f"turn {r['turn']}: combined probability is the product of the three factors",
            abs(r["probability"] - (r["draw_factor"] * r["land_factor"] * r["color_factor"])) < 1e-9,
        )
    check(
        "draw_factor matches a plain prob_at_least() call for the card's own quantity",
        abs(rows[0]["draw_factor"] - prob.prob_at_least(99, 1, 7, 1)) < 1e-9,
    )
    check(
        "probability is non-decreasing turn-over-turn (more cards seen can only help)",
        all(rows[i]["probability"] <= rows[i + 1]["probability"] + 1e-9 for i in range(len(rows) - 1)),
    )

    # A free (cmc=0, no colored pips) card should have land_factor ==
    # color_factor == 1.0 at every turn — only the draw factor matters.
    free_rows = prob.card_cast_probability_by_turn(
        library_size=99, land_count=35, source_counts={}, card_qty=1,
        card_cmc=0, colored_pips={}, on_the_play=True, max_turn=8,
    )
    check(
        "a 0-mana-value card with no colored pips has land_factor == color_factor == 1.0 throughout",
        all(r["land_factor"] == 1.0 and r["color_factor"] == 1.0 for r in free_rows),
    )
    check(
        "...so its combined probability equals its draw_factor exactly",
        all(abs(r["probability"] - r["draw_factor"]) < 1e-9 for r in free_rows),
    )

    # Two otherwise-identical singleton cards with DIFFERENT mana costs
    # must now produce DIFFERENT curves — this is the actual bug being
    # fixed (old tool showed identical curves for every singleton).
    cheap_rows = prob.card_cast_probability_by_turn(
        library_size=99, land_count=35, source_counts={"R": 12}, card_qty=1,
        card_cmc=1, colored_pips={"R": 1}, on_the_play=True, max_turn=8,
    )
    pricey_rows = prob.card_cast_probability_by_turn(
        library_size=99, land_count=35, source_counts={"R": 12}, card_qty=1,
        card_cmc=6, colored_pips={"R": 1}, on_the_play=True, max_turn=8,
    )
    check(
        "a 1-drop and a 6-drop singleton no longer show identical turn-3 probabilities",
        abs(cheap_rows[3]["probability"] - pricey_rows[3]["probability"]) > 1e-6,
    )
    check(
        "the cheaper card is easier to play on curve by turn 3",
        cheap_rows[3]["probability"] > pricey_rows[3]["probability"],
    )

    # ------------------------------------------------------------------
    # 3. expected_count_by_turn / weighted_mana_expectation
    # ------------------------------------------------------------------
    check("expected_count_by_turn: zero library size returns 0.0", prob.expected_count_by_turn(0, 33, 3, True) == 0.0)
    exp_hand = prob.expected_count_by_turn(99, 33, 0, True)
    check("expected_count_by_turn: opening hand matches linearity-of-expectation math", abs(exp_hand - 33 * 7 / 99) < 1e-9)

    cats = {"Rocks/Dorks": 5, "Colorless": 2, "Any Color": 1, "W": 3, "U": 0, "B": 0, "R": 0, "G": 0}
    weighted = prob.weighted_mana_expectation(99, cats, 4, True)
    check(
        "weighted_mana_expectation matches expected_count_by_turn per category",
        all(abs(weighted[cat] - prob.expected_count_by_turn(99, total, 4, True)) < 1e-9 for cat, total in cats.items()),
    )
    zero_lib_weighted = prob.weighted_mana_expectation(0, cats, 4, True)
    check("weighted_mana_expectation: zero library size returns all zeros", all(v == 0.0 for v in zero_lib_weighted.values()))

    # ------------------------------------------------------------------
    # 4. EDHREC Salt Score removal
    # ------------------------------------------------------------------
    check("CARD_COLUMNS no longer selects cards.edhrec_salt", "edhrec_salt" not in q.CARD_COLUMNS)
    check("queries.deck_salt_top10 was removed", not hasattr(q, "deck_salt_top10"))
    check("writes.set_card_salt_score was removed", not hasattr(w, "set_card_salt_score"))
    check("writes.bulk_set_salt_scores was removed", not hasattr(w, "bulk_set_salt_scores"))
    check("loaders.load_deck_salt_top10 was removed", not hasattr(ld, "load_deck_salt_top10"))

    # schema.sql must still declare the column (additive/non-destructive
    # policy — never drop a column, even a retired one).
    conn = sqlite3.connect(":memory:")
    with open(os.path.join(PROJECT_ROOT, "schema.sql")) as f:
        conn.executescript(f.read())
    cols = {row[1] for row in conn.execute("PRAGMA table_info(cards)").fetchall()}
    check("schema.sql still physically declares cards.edhrec_salt (non-destructive policy)", "edhrec_salt" in cols)

    # And confirm the app-level query genuinely never reads it back, even
    # against a database with a value manually stashed in that column.
    conn.execute(
        "INSERT INTO cards (scryfall_id, name, set_code, collector_number, edhrec_salt) "
        "VALUES ('t1', 'Test Card', 'TST', '1', 4.2)"
    )
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Test Deck')")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1, 't1', 1)")
    conn.commit()
    deck_df = q.deck_cards_dataframe(conn, 1)
    check("deck_cards_dataframe() output has NO edhrec_salt column", "edhrec_salt" not in deck_df.columns)
    coll_df = q.collection_dataframe(conn)
    check(
        "collection_dataframe() output has NO edhrec_salt column (even with an empty collection)",
        "edhrec_salt" not in coll_df.columns,
    )

    print("\nAll Prompt Pass 5 offline checks passed.")


if __name__ == "__main__":
    main()

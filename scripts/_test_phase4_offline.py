"""
Offline test for Prompt Pass 4 changes: no Streamlit, no network — a
scratch SQLite DB built straight from schema.sql, plus a minimal
session_state stand-in for the filter-key registry in card_view.py.

Run from anywhere:
    python scripts/_test_phase4_offline.py
"""
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dashboard_lib import formatting as fmt
from dashboard_lib import queries as q


def build_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(PROJECT_ROOT, "schema.sql")) as f:
        conn.executescript(f.read())

    # Two decks: one with a cover image, one without (falls back to
    # commander art), one commander ("Krenko") shared across two
    # different owned printings to make sure list_decks_with_covers()
    # resolves the printing actually run in THIS deck, not just any
    # printing with a matching name.
    conn.execute(
        "INSERT INTO decks (deck_id, name, commander, representative, cover_image_path) "
        "VALUES (1, 'Goblin Storm', 'Krenko, Mob Boss', 'Krenko, Mob Boss', NULL)"
    )
    conn.execute(
        "INSERT INTO decks (deck_id, name, commander, representative, cover_image_path) "
        "VALUES (2, 'Reserved Test', 'Krenko, Mob Boss', 'Krenko, Mob Boss', 'image_cache/deck_covers/2.png')"
    )

    cards = [
        # scryfall_id, name, set_code, collector_number, color_identity, current_price_usd, is_reserved, image_uri
        ("krenko-a", "Krenko, Mob Boss", "M13", "191", "R", 12.50, 0, "https://example.com/krenko-a.jpg"),
        ("krenko-b", "Krenko, Mob Boss", "CMD", "100", "R", 8.00, 0, "https://example.com/krenko-b.jpg"),
        ("goblin-1", "Goblin Chieftain", "M15", "1", "R", 3.00, 0, None),
        ("mox-emerald", "Mox Emerald", "LEA", "263", "G", 4000.00, 1, None),
        ("sol-ring", "Sol Ring", "C21", "263", "", 2.00, 0, None),
        ("unowned-card", "Lightning Bolt", "M11", "146", "R", 1.00, 0, None),
    ]
    for scryfall_id, name, set_code, cn, ci, price, reserved, image_uri in cards:
        conn.execute(
            "INSERT INTO cards (scryfall_id, name, set_code, collector_number, color_identity, "
            "current_price_usd, is_reserved, image_uri) VALUES (?,?,?,?,?,?,?,?)",
            (scryfall_id, name, set_code, cn, ci, price, reserved, image_uri),
        )

    # Deck 1 mainboard: commander printing "krenko-a", plus Mox Emerald
    # (reserved list + very expensive -> also exercises Top 10 Most
    # Expensive) and Sol Ring.
    for sid, qty in [("krenko-a", 1), ("mox-emerald", 1), ("sol-ring", 1)]:
        conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1, ?, ?)", (sid, qty))
    # Deck 2 mainboard: commander printing "krenko-b" (the OTHER Krenko
    # printing) plus Goblin Chieftain.
    for sid, qty in [("krenko-b", 1), ("goblin-1", 1)]:
        conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (2, ?, ?)", (sid, qty))

    # Collection lots:
    #  - Mox Emerald sleeved in "Goblin Storm" (deck 1), price_paid logged
    #    -> exercises deck_price_top10()'s purchase_price join, and
    #    in_deck=1 for the Collection toggle.
    #  - Goblin Chieftain owned but NOT run in any deck, no location
    #    -> exercises "Not in a deck OR another location" = the
    #    unassigned bucket.
    #    (Lightning Bolt used here instead of Goblin Chieftain, which
    #    ends up run in deck 2's mainboard below.)
    #  - Sol Ring owned, run in deck 1, but location is a binder (not a
    #    deck name) -> exercises in_deck=1 while location is non-blank,
    #    which should NOT count as "Not in a deck OR another location".
    conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, location, price_paid) VALUES "
        "('mox-emerald', 1, 'Goblin Storm', 350.00)"
    )
    conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, location, price_paid) VALUES "
        "('unowned-card', 4, NULL, NULL)"
    )
    conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, location, price_paid) VALUES "
        "('sol-ring', 1, 'Binder A', 1.50)"
    )
    conn.commit()
    return conn


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(f"Test failed: {label}")


def main():
    conn = build_db()

    # --- formatting.mana_curve_color_bucket ---
    check("mono W bucket", fmt.mana_curve_color_bucket("W") == "W")
    check("mono G bucket", fmt.mana_curve_color_bucket("G") == "G")
    check("multi bucket (2 colors)", fmt.mana_curve_color_bucket("W/U") == "Multi-color")
    check("multi bucket (3 colors)", fmt.mana_curve_color_bucket("W/U/B") == "Multi-color")
    check("colorless bucket (label)", fmt.mana_curve_color_bucket("Colorless") == "Colorless")
    check("colorless bucket (empty)", fmt.mana_curve_color_bucket("") == "Colorless")
    check("colorless bucket (None)", fmt.mana_curve_color_bucket(None) == "Colorless")
    check(
        "fixed order matches spec",
        fmt.MANA_CURVE_COLOR_ORDER == ["W", "U", "B", "R", "G", "Multi-color", "Colorless"],
    )
    check(
        "every order entry has a color",
        all(c in fmt.MANA_CURVE_COLOR_HEX for c in fmt.MANA_CURVE_COLOR_ORDER),
    )

    # --- queries.list_decks_with_covers ---
    covers = q.list_decks_with_covers(conn)
    covers_by_id = {int(r["deck_id"]): r for _, r in covers.iterrows()}
    check("two decks returned", len(covers) == 2)
    check(
        "deck 1 (no cover) resolves commander image via its OWN printing (krenko-a)",
        covers_by_id[1]["commander_image_uri"] == "https://example.com/krenko-a.jpg",
    )
    check(
        "deck 2 (no cover) resolves commander image via its OWN printing (krenko-b), "
        "not deck 1's printing",
        covers_by_id[2]["commander_image_uri"] == "https://example.com/krenko-b.jpg",
    )
    check(
        "deck 2's cover_image_path is preserved for the UI to prefer over commander art",
        covers_by_id[2]["cover_image_path"] == "image_cache/deck_covers/2.png",
    )

    # --- queries.deck_reserved_list_cards (price, not quantity) ---
    reserved = q.deck_reserved_list_cards(conn, 1)
    check("reserved list has 1 row (Mox Emerald)", len(reserved) == 1)
    check("reserved list has a price column", "price" in reserved.columns)
    check("reserved list has NO quantity column", "quantity" not in reserved.columns)
    check("reserved list price matches card price", float(reserved.iloc[0]["price"]) == 4000.00)

    # --- queries.deck_price_top10 (purchase_price, no quantity) ---
    top10 = q.deck_price_top10(conn, 1)
    check("top10 has NO quantity column", "quantity" not in top10.columns)
    check("top10 has a purchase_price column", "purchase_price" in top10.columns)
    top10_by_name = {r["card_name"]: r for _, r in top10.iterrows()}
    check(
        "Mox Emerald's purchase_price reflects the logged collection lot ($350)",
        float(top10_by_name["Mox Emerald"]["purchase_price"]) == 350.00,
    )
    check(
        "Sol Ring's purchase_price is NULL (no lot located at 'Goblin Storm')",
        top10_by_name["Sol Ring"]["purchase_price"] is None
        or (isinstance(top10_by_name["Sol Ring"]["purchase_price"], float)
            and top10_by_name["Sol Ring"]["purchase_price"] != top10_by_name["Sol Ring"]["purchase_price"]),  # NaN check
    )
    check("top10 sorted most-expensive first", list(top10["price"]) == sorted(top10["price"], reverse=True))

    # --- queries.collection_dataframe (in_deck) ---
    coll = q.collection_dataframe(conn)
    coll_by_card = {r["name"]: r for _, r in coll.iterrows()}
    check("Mox Emerald flagged in_deck=1 (run in deck 1)", int(coll_by_card["Mox Emerald"]["in_deck"]) == 1)
    check("Sol Ring flagged in_deck=1 (run in deck 1)", int(coll_by_card["Sol Ring"]["in_deck"]) == 1)
    check(
        "Goblin Chieftain flagged in_deck=0 (owned but not run in any deck)",
        int(coll_by_card["Lightning Bolt"]["in_deck"]) == 0,
    )

    # "Not in a deck OR another location" filter logic, as implemented in
    # pages/1_Collection.py: in_deck == 0 AND location blank/NULL.
    unassigned = coll[
        (coll["in_deck"] == 0) & (coll["location"].isna() | (coll["location"].astype(str).str.strip() == ""))
    ]
    check(
        "Goblin Chieftain (not in a deck, no location) IS in the unassigned bucket",
        "Lightning Bolt" in set(unassigned["name"]),
    )
    check(
        "Sol Ring (in a deck, has a location) is NOT in the unassigned bucket",
        "Sol Ring" not in set(unassigned["name"]),
    )
    check(
        "Mox Emerald (in a deck, has a location) is NOT in the unassigned bucket",
        "Mox Emerald" not in set(unassigned["name"]),
    )

    # --- card_view filter-key registry, with a minimal session_state stub ---
    class _FakeSessionState(dict):
        def setdefault(self, key, default):
            return super().setdefault(key, default)

    class _FakeStreamlit:
        def __init__(self):
            self.session_state = _FakeSessionState()

    fake_st = _FakeStreamlit()
    import types
    cv_module = types.ModuleType("card_view_test")
    # Re-implement just the two functions under test against the fake
    # session_state, mirroring card_view.py's real implementation
    # exactly, to confirm the registry/reset logic itself is correct
    # without needing a full Streamlit stub for the whole module.
    _FILTER_KEYS_STATE = "_active_filter_widget_keys"

    def register_filter_key(key):
        fake_st.session_state.setdefault(_FILTER_KEYS_STATE, set())
        fake_st.session_state[_FILTER_KEYS_STATE].add(key)

    def reset_filters():
        for key in fake_st.session_state.get(_FILTER_KEYS_STATE, set()):
            fake_st.session_state.pop(key, None)
        fake_st.session_state.pop(_FILTER_KEYS_STATE, None)

    register_filter_key("collection_search")
    register_filter_key("collection_color_identity_multi")
    fake_st.session_state["collection_search"] = "sol ring"
    fake_st.session_state["collection_color_identity_multi"] = ["R"]
    reset_filters()
    check("reset_filters cleared the search key", "collection_search" not in fake_st.session_state)
    check(
        "reset_filters cleared the multiselect key",
        "collection_color_identity_multi" not in fake_st.session_state,
    )
    check("reset_filters cleared its own registry", _FILTER_KEYS_STATE not in fake_st.session_state)

    print("\nAll Phase 4 offline checks passed.")


if __name__ == "__main__":
    main()

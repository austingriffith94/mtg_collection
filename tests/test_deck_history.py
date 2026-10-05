"""Change history, deck comparison and tile status strips — Phase 5 of the
Workbench rework (see IMPLEMENTATION_PLAN.md).
"""
import pandas as pd

from dashboard_lib import card_view
from dashboard_lib import queries as q


def _deck(conn, name):
    cur = conn.execute("INSERT INTO decks (name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def _change(conn, deck_id, status, add=None, remove=None, resolved_at=None, notes=None):
    cur = conn.execute(
        """INSERT INTO deck_changes (deck_id, status, add_scryfall_id, remove_scryfall_id,
                                     resolved_at, notes)
           VALUES (?,?,?,?,?,?)""",
        (deck_id, status, add, remove, resolved_at, notes),
    )
    conn.commit()
    return cur.lastrowid


def _game(conn, date, *participants):
    """participants: (deck_id, is_winner) pairs."""
    gid = conn.execute("INSERT INTO games (date) VALUES (?)", (date,)).lastrowid
    for seat, (deck_id, won) in enumerate(participants, start=1):
        conn.execute(
            "INSERT INTO game_participants (game_id, deck_name, deck_id, is_winner, seat)"
            " VALUES (?,?,?,?,?)",
            (gid, f"deck{deck_id}", deck_id, int(won), seat),
        )
    conn.commit()


def _changes(history):
    return [h for h in history if h["kind"] == "change"]


def _games_rows(history):
    return [h for h in history if h["kind"] == "games"]


# ---------------------------------------------------------------- history

def test_deck_with_no_history_is_an_empty_list(conn):
    d = _deck(conn, "Raktres")
    assert q.deck_change_history(conn, d) == []


def test_history_is_newest_first_and_includes_applied_and_dropped(conn, add_card):
    d = _deck(conn, "Raktres")
    a, b, c = add_card("Sol Ring"), add_card("Blood Crypt"), add_card("Jet Medallion")
    _change(conn, d, "applied", add=a, resolved_at="2026-09-13 10:00:00")
    _change(conn, d, "dropped", add=b, resolved_at="2026-09-20 10:00:00")
    _change(conn, d, "applied", add=c, resolved_at="2026-10-04 10:00:00")
    _change(conn, d, "idea", add=add_card("Open Idea"))  # open rows never appear

    got = _changes(q.deck_change_history(conn, d))
    assert [(h["add_card"], h["status"]) for h in got] == [
        ("Jet Medallion", "applied"), ("Blood Crypt", "dropped"), ("Sol Ring", "applied"),
    ]
    assert [h["date"] for h in got] == ["2026-10-04", "2026-09-20", "2026-09-13"]


def test_history_shows_both_sides_of_a_swap(conn, add_card):
    d = _deck(conn, "Raktres")
    _change(conn, d, "applied", add=add_card("Undead Warchief"), remove=add_card("Heirloom Blade"),
            resolved_at="2026-10-04 10:00:00")
    (h,) = _changes(q.deck_change_history(conn, d))
    assert (h["add_card"], h["remove_card"]) == ("Undead Warchief", "Heirloom Blade")


def test_games_since_latest_change_counts_only_later_games(conn, add_card):
    d = _deck(conn, "Raktres")
    _change(conn, d, "applied", add=add_card("Sol Ring"), resolved_at="2026-09-13 10:00:00")
    _game(conn, "2026-09-01", (d, True))    # before the change
    _game(conn, "2026-09-13", (d, True))    # same day: counts against the OLDER list
    _game(conn, "2026-09-20", (d, False))
    _game(conn, "2026-09-27", (d, False))

    top = _games_rows(q.deck_change_history(conn, d))[0]
    assert (top["games"], top["wins"], top["losses"]) == (2, 0, 2)


def test_games_of_other_decks_do_not_count(conn, add_card):
    d, other = _deck(conn, "Raktres"), _deck(conn, "Marchesa")
    _change(conn, d, "applied", add=add_card("Sol Ring"), resolved_at="2026-09-13 10:00:00")
    _game(conn, "2026-09-20", (other, True), (d, False))
    top = _games_rows(q.deck_change_history(conn, d))[0]
    assert (top["games"], top["wins"], top["losses"]) == (1, 0, 1)


def test_games_between_two_changes_sit_between_them(conn, add_card):
    d = _deck(conn, "Raktres")
    _change(conn, d, "applied", add=add_card("Sol Ring"), resolved_at="2026-09-01 10:00:00")
    _change(conn, d, "applied", add=add_card("Jet Medallion"), resolved_at="2026-10-01 10:00:00")
    _game(conn, "2026-09-10", (d, True))
    _game(conn, "2026-09-20", (d, False))
    _game(conn, "2026-10-01", (d, True))    # day of the newer change: older list
    _game(conn, "2026-10-02", (d, True))    # after the newer change

    history = q.deck_change_history(conn, d)
    kinds = [h["kind"] for h in history]
    assert kinds == ["games", "change", "games", "change"]
    since_latest, between = _games_rows(history)
    assert (since_latest["games"], since_latest["wins"]) == (1, 1)
    assert (between["games"], between["wins"], between["losses"]) == (3, 2, 1)


def test_two_changes_on_one_day_share_one_period(conn, add_card):
    d = _deck(conn, "Raktres")
    _change(conn, d, "applied", add=add_card("Sol Ring"), resolved_at="2026-10-04 09:00:00")
    _change(conn, d, "applied", add=add_card("Jet Medallion"), resolved_at="2026-10-04 11:00:00")
    # One "since" line, not a zero-width "between" line splitting the pair.
    assert [h["kind"] for h in q.deck_change_history(conn, d)] == ["games", "change", "change"]


def test_dropped_rows_do_not_start_a_period(conn, add_card):
    d = _deck(conn, "Raktres")
    _change(conn, d, "applied", add=add_card("Sol Ring"), resolved_at="2026-09-01 10:00:00")
    _change(conn, d, "dropped", add=add_card("Blood Crypt"), resolved_at="2026-10-01 10:00:00")
    _game(conn, "2026-09-20", (d, True))
    history = q.deck_change_history(conn, d)
    (only,) = _games_rows(history)
    assert only["games"] == 1   # the drop didn't cut the period in two


def test_only_dropped_history_has_no_games_line(conn, add_card):
    d = _deck(conn, "Raktres")
    _change(conn, d, "dropped", add=add_card("Blood Crypt"), resolved_at="2026-10-01 10:00:00")
    _game(conn, "2026-10-02", (d, True))
    assert _games_rows(q.deck_change_history(conn, d)) == []


# ------------------------------------------------------------- comparison

def test_comparison_has_a_row_per_deck_even_with_nothing_logged(conn):
    d1, d2 = _deck(conn, "Raktres"), _deck(conn, "Marchesa")
    df = q.deck_comparison(conn)
    assert sorted(df["name"]) == ["Marchesa", "Raktres"]
    row = df[df["deck_id"] == d1].iloc[0]
    assert (row["games"], row["planned"], row["ideas"]) == (0, 0, 0)
    assert pd.isna(row["win_rate"]) and pd.isna(row["avg_cmc"])
    assert pd.isna(row["combos"])   # never checked, not zero
    assert d2 in set(df["deck_id"])


def test_comparison_counts_open_changes_record_and_avg_cmc(conn, add_card):
    d = _deck(conn, "Raktres")
    sol, bolt, land = add_card("Sol Ring"), add_card("Lightning Bolt"), add_card("Mountain")
    conn.execute("UPDATE cards SET cmc=1, type_line='Artifact' WHERE scryfall_id=?", (sol,))
    conn.execute("UPDATE cards SET cmc=3, type_line='Instant' WHERE scryfall_id=?", (bolt,))
    conn.execute("UPDATE cards SET cmc=0, type_line='Basic Land — Mountain' WHERE scryfall_id=?", (land,))
    for sid, qty in ((sol, 1), (bolt, 1), (land, 8)):
        conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,?)", (d, sid, qty))
    _change(conn, d, "planned", add=add_card("A"), remove=bolt)
    _change(conn, d, "idea", add=add_card("B"))
    _change(conn, d, "idea", add=add_card("C"))
    _change(conn, d, "applied", add=add_card("D"), resolved_at="2026-10-01 00:00:00")  # not open
    _game(conn, "2026-10-02", (d, True))
    _game(conn, "2026-10-03", (d, False))
    _game(conn, "2026-10-04", (d, False))

    row = q.deck_comparison(conn).iloc[0]
    assert (row["planned"], row["ideas"]) == (1, 2)
    assert (row["games"], row["wins"], row["losses"]) == (3, 1, 2)
    assert abs(row["win_rate"] - 1 / 3) < 0.01
    assert row["avg_cmc"] == 2.0   # lands excluded: (1 + 3) / 2


def test_comparison_build_pct_follows_reconciliation(conn, add_card):
    d = _deck(conn, "Raktres")
    for name, loc in (("Sol Ring", "Raktres"), ("Blood Crypt", "Box")):
        sid = add_card(name)
        conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (d, sid))
        conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES (?,1,?)", (sid, loc))
    conn.commit()
    assert q.deck_comparison(conn).iloc[0]["build_pct"] == 50.0


# ------------------------------------------------------------ tile strips

def test_status_strip_omits_what_does_not_apply():
    assert card_view.deck_status_strip({"planned": 9, "ideas": 18, "games": 3, "wins": 1, "losses": 2,
                                        "build_pct": 100.0}) == "9 planned · 18 ideas · 1-2"
    assert card_view.deck_status_strip({"planned": 0, "ideas": 1, "games": 0, "wins": 0, "losses": 0,
                                        "build_pct": None}) == "1 idea"
    assert card_view.deck_status_strip({"planned": 0, "ideas": 0, "games": 0, "build_pct": 100.0}) == ""


def test_status_strip_shows_build_only_while_unfinished():
    row = {"planned": 0, "ideas": 0, "games": 0, "build_pct": 4.2}
    assert card_view.deck_status_strip(row) == "4% built"
    # A NaN (pandas' None) must read as absent, not as "nan% built".
    assert card_view.deck_status_strip({**row, "build_pct": float("nan")}) == ""

"""
Offline test for Prompt Pass 7 changes: no Streamlit, no live network.

Covers:
  1. writes.create_game()/update_game() validation — exactly one winner
     required, incomplete participant lists rejected, update_game()
     replaces a game's participants in place.
  2. writes.delete_deck()'s "Tracked Deck Deletion" audit — confirms the
     existing detach-not-delete behavior for game_participants, plus the
     is_own_deck=0 hygiene fix made during this pass.
  3. queries.game_detail() — the Edit form's data source.
  4. queries.distinct_player_names() / distinct_untracked_deck_names()
     — the autofill dropdown sources, including the post-deck-deletion
     case.
  5. queries.player_elo_ratings() — the multiplayer ELO variant, using
     the exact zero-sum pairwise-update math to assert precise rating
     values, not just directional sanity.
  6. queries.player_head_to_head_matrix() — pairwise win-rate crosstab.

Run from anywhere:
    python scripts/_test_prompt_pass7_offline.py
"""
import os
import sqlite3
import sys
import types

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# dashboard_lib.loaders has a real `import streamlit` (for @st.cache_data);
# streamlit isn't installed in this offline sandbox (see PROJECT_STATE.md's
# testing caveat), so register a minimal stub before importing anything
# that pulls loaders in transitively — same trick prior _test_*_offline.py
# files use.
if "streamlit" not in sys.modules:
    _fake_streamlit = types.ModuleType("streamlit")

    def _fake_cache_data(*args, **kwargs):
        def _decorator(func):
            func.clear = lambda: None
            return func
        return _decorator

    _fake_streamlit.cache_data = _fake_cache_data
    sys.modules["streamlit"] = _fake_streamlit

from dashboard_lib import queries as q
from dashboard_lib import writes as w
from dashboard_lib import game_form as gf


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(f"Test failed: {label}")


def fresh_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(PROJECT_ROOT, "schema.sql")) as f:
        conn.executescript(f.read())
    return conn


def main():
    # ------------------------------------------------------------------
    # 0. game_form.resolve_seat() / validate_and_build_participants() —
    #    the page's pure form-logic layer, extracted out to dashboard_lib
    #    specifically so it's testable without a real Streamlit runtime.
    # ------------------------------------------------------------------
    label_to_id = {"My Deck": 1, "Their Deck": 2}
    FT, NEW = gf.FREE_TEXT_OPTION, gf.NEW_ENTRY_OPTION

    # A fully blank seat resolves to nothing and is NOT "used".
    deck_name, deck_id, player_name, used = gf.resolve_seat(FT, NEW, "", NEW, "", False, label_to_id)
    check("a fully blank seat is not 'used'", used is False)
    check("a fully blank seat has no deck", deck_name is None)

    # A tracked deck pick wins over everything else.
    deck_name, deck_id, player_name, used = gf.resolve_seat("My Deck", NEW, "ignored", NEW, "", False, label_to_id)
    check("tracked-deck dropdown resolves to that deck's id", deck_name == "My Deck" and deck_id == 1)
    check("picking a tracked deck marks the seat used", used is True)

    # Known-opponent-deck dropdown, when tracked deck is left on free-text.
    deck_name, deck_id, player_name, used = gf.resolve_seat(FT, "Rakdos Showstopper", "", NEW, "", False, label_to_id)
    check("known-opponent dropdown resolves with deck_id None", deck_name == "Rakdos Showstopper" and deck_id is None)

    # Free-text opponent deck name, when neither dropdown is used.
    deck_name, deck_id, player_name, used = gf.resolve_seat(FT, NEW, "  Brand New Opponent Deck  ", NEW, "", False, label_to_id)
    check("free-text opponent deck name is used (and stripped) as a last resort", deck_name == "Brand New Opponent Deck" and deck_id is None)

    # Player: known dropdown wins over free text; free text is the fallback.
    _, _, player_name, _ = gf.resolve_seat(FT, NEW, "", "Anthony", "ignored", False, label_to_id)
    check("known-player dropdown takes precedence over free text", player_name == "Anthony")
    _, _, player_name, _ = gf.resolve_seat(FT, NEW, "", NEW, "  Brand New Player  ", False, label_to_id)
    check("free-text player name is used (and stripped) when dropdown is on its sentinel", player_name == "Brand New Player")

    # A seat with SOME info (player + Won) but no resolvable deck is
    # still flagged 'used' — validate_and_build_participants() below is
    # what turns that into a rejection, but resolve_seat() itself must
    # not silently treat it as blank.
    deck_name, deck_id, player_name, used = gf.resolve_seat(FT, NEW, "", "Anthony", "", True, label_to_id)
    check("a seat with a player + Won but no deck is still 'used' (not silently dropped)", used is True and deck_name is None)

    # validate_and_build_participants(): the "half-filled seat" case is rejected with a helpful message.
    half_filled_seats = [
        (FT, NEW, "", "Anthony", "", True),  # seat 1: player + won, no deck
        (FT, NEW, "", NEW, "", False),        # seat 2: blank, skipped
        (FT, NEW, "", NEW, "", False),        # seat 3: blank, skipped
        (FT, NEW, "", NEW, "", False),        # seat 4: blank, skipped
    ]
    participants, err = gf.validate_and_build_participants(half_filled_seats, label_to_id)
    check("a half-filled seat (player+won, no deck) is rejected, not silently dropped", participants is None and "Seat 1" in err)

    # A fully valid 2-seat submission with exactly one winner succeeds.
    good_seats = [
        ("My Deck", NEW, "", "Me", "", True),
        (FT, NEW, "Free Text Opponent", "Anthony", "", False),
        (FT, NEW, "", NEW, "", False),
        (FT, NEW, "", NEW, "", False),
    ]
    participants, err = gf.validate_and_build_participants(good_seats, label_to_id)
    check("a valid 2-seat submission with one winner passes validation", err is None and len(participants) == 2)
    check("the tracked-deck seat resolved correctly", participants[0] == {"deck_name": "My Deck", "deck_id": 1, "is_winner": True, "player_name": "Me"})
    check("the free-text seat resolved correctly", participants[1] == {"deck_name": "Free Text Opponent", "deck_id": None, "is_winner": False, "player_name": "Anthony"})

    # All-blank submission is rejected as "nothing to log".
    all_blank = [(FT, NEW, "", NEW, "", False)] * 4
    participants, err = gf.validate_and_build_participants(all_blank, label_to_id)
    check("an all-blank submission is rejected", participants is None and "at least one" in err.lower())

    # Zero winners among otherwise-valid seats is rejected.
    no_winner_seats = [
        ("My Deck", NEW, "", "Me", "", False),
        (FT, NEW, "Free Text Opponent", "Anthony", "", False),
        (FT, NEW, "", NEW, "", False),
        (FT, NEW, "", NEW, "", False),
    ]
    participants, err = gf.validate_and_build_participants(no_winner_seats, label_to_id)
    check("zero winners among filled seats is rejected", participants is None and "winner" in err.lower())

    # Two winners is rejected.
    two_winner_seats = [
        ("My Deck", NEW, "", "Me", "", True),
        (FT, NEW, "Free Text Opponent", "Anthony", "", True),
        (FT, NEW, "", NEW, "", False),
        (FT, NEW, "", NEW, "", False),
    ]
    participants, err = gf.validate_and_build_participants(two_winner_seats, label_to_id)
    check("two winners is rejected", participants is None and "one seat" in err.lower())

    # ------------------------------------------------------------------
    # 1. create_game() / update_game() validation
    # ------------------------------------------------------------------
    conn = fresh_conn()
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'My Deck')")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (2, 'Their Deck')")
    conn.commit()

    # No winner at all -> rejected (this is the Prompt Pass 7 behavior
    # change: a 0-winner "draw" is no longer accepted from the dashboard,
    # though older migrate.py-imported draws are untouched since that
    # script inserts directly and never calls create_game()).
    gid, err = w.create_game(
        conn, date="2026-01-01", note=None,
        participants=[
            {"deck_name": "My Deck", "deck_id": 1, "is_winner": False, "player_name": "Me"},
            {"deck_name": "Their Deck", "deck_id": 2, "is_winner": False, "player_name": "Anthony"},
        ],
    )
    check("create_game rejects a game with zero winners", gid is None and err is not None)
    check("games table is still empty after the rejected insert", conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0)

    # Two winners -> still rejected (pre-existing rule, unchanged).
    gid, err = w.create_game(
        conn, date="2026-01-01", note=None,
        participants=[
            {"deck_name": "My Deck", "deck_id": 1, "is_winner": True, "player_name": "Me"},
            {"deck_name": "Their Deck", "deck_id": 2, "is_winner": True, "player_name": "Anthony"},
        ],
    )
    check("create_game rejects a game with two winners", gid is None and err is not None)

    # No usable participants at all -> rejected (pre-existing rule).
    gid, err = w.create_game(conn, date="2026-01-01", note=None, participants=[])
    check("create_game rejects an empty participant list", gid is None and err is not None)

    # Exactly one winner -> accepted.
    gid, err = w.create_game(
        conn, date="2026-01-01", note="first game",
        participants=[
            {"deck_name": "My Deck", "deck_id": 1, "is_winner": True, "player_name": "Me"},
            {"deck_name": "Their Deck", "deck_id": 2, "is_winner": False, "player_name": "Anthony"},
        ],
    )
    check("create_game accepts a game with exactly one winner", gid is not None and err is None)

    seats = conn.execute(
        "SELECT seat, deck_name, deck_id, is_winner, player_name FROM game_participants WHERE game_id=? ORDER BY seat",
        (gid,),
    ).fetchall()
    check("exactly 2 participant rows were inserted", len(seats) == 2)
    check("seats are numbered 1..N by list position", [s["seat"] for s in seats] == [1, 2])

    # update_game(): replace date/note/participants entirely.
    ok, err = w.update_game(
        conn, gid, date="2026-01-02", note="corrected",
        participants=[
            {"deck_name": "My Deck", "deck_id": 1, "is_winner": False, "player_name": "Me"},
            {"deck_name": "Their Deck", "deck_id": 2, "is_winner": True, "player_name": "Anthony"},
            {"deck_name": "Some Free Text Deck", "deck_id": None, "is_winner": False, "player_name": "Joe G"},
        ],
    )
    check("update_game succeeds with a valid replacement participant list", ok is True and err is None)

    updated = conn.execute("SELECT date, note FROM games WHERE game_id=?", (gid,)).fetchone()
    check("update_game changed the date", updated["date"] == "2026-01-02")
    check("update_game changed the note", updated["note"] == "corrected")

    new_seats = conn.execute(
        "SELECT seat, deck_name, deck_id, is_winner, player_name FROM game_participants WHERE game_id=? ORDER BY seat",
        (gid,),
    ).fetchall()
    check("update_game replaced 2 participants with 3", len(new_seats) == 3)
    check("update_game re-numbered seats 1..3", [s["seat"] for s in new_seats] == [1, 2, 3])
    check("the new winner is Anthony/Their Deck", new_seats[1]["is_winner"] == 1 and new_seats[1]["player_name"] == "Anthony")
    check("a free-text (untracked) seat has deck_id NULL", new_seats[2]["deck_id"] is None)

    # update_game() validation: same rules apply.
    ok, err = w.update_game(
        conn, gid, date="2026-01-02", note=None,
        participants=[{"deck_name": "My Deck", "deck_id": 1, "is_winner": False, "player_name": "Me"}],
    )
    check("update_game rejects a replacement with zero winners", ok is False and err is not None)

    ok, err = w.update_game(conn, 99999, date="2026-01-02", note=None, participants=[])
    check("update_game on a nonexistent game_id returns an error", ok is False and err == "Game not found.")

    # ------------------------------------------------------------------
    # 2. delete_deck() "Tracked Deck Deletion" audit
    # ------------------------------------------------------------------
    detail_before = q.game_detail(conn, gid)
    my_deck_seat = next(p for p in detail_before["participants"] if p["deck_name"] == "My Deck")
    check("sanity: My Deck's participant row is tracked (deck_id set) before deletion", my_deck_seat["deck_id"] == 1)

    deleted_name, del_err = w.delete_deck(conn, 1)
    check("delete_deck succeeds", deleted_name == "My Deck" and del_err is None)
    check("the deck row itself is gone", conn.execute("SELECT 1 FROM decks WHERE deck_id=1").fetchone() is None)
    check("the game itself still exists (not deleted)", conn.execute("SELECT 1 FROM games WHERE game_id=?", (gid,)).fetchone() is not None)

    detail_after = q.game_detail(conn, gid)
    my_deck_seat_after = next(p for p in detail_after["participants"] if p["deck_name"] == "My Deck")
    check("the detached seat's deck_name text survives", my_deck_seat_after["deck_name"] == "My Deck")
    check("the detached seat's deck_id is now NULL", my_deck_seat_after["deck_id"] is None)
    is_own_deck_after = conn.execute(
        "SELECT is_own_deck FROM game_participants WHERE game_id=? AND deck_name='My Deck'", (gid,)
    ).fetchone()[0]
    check("Prompt Pass 7 hygiene fix: is_own_deck is cleared to 0 on detach", is_own_deck_after == 0)

    other_seat_after = next(p for p in detail_after["participants"] if p["deck_name"] == "Their Deck")
    check("the OTHER (non-deleted) deck's seat is untouched", other_seat_after["deck_id"] == 2 and other_seat_after["is_winner"] is True)

    # ------------------------------------------------------------------
    # 3. distinct_player_names / distinct_untracked_deck_names
    # ------------------------------------------------------------------
    players = q.distinct_player_names(conn)
    check("distinct_player_names includes everyone logged", set(players) == {"Me", "Anthony", "Joe G"})
    check("distinct_player_names is sorted", players == sorted(players, key=str.lower))

    untracked = q.distinct_untracked_deck_names(conn)
    check(
        "distinct_untracked_deck_names includes the always-free-text deck AND the now-detached former-tracked deck",
        set(untracked) == {"Some Free Text Deck", "My Deck"},
    )

    # ------------------------------------------------------------------
    # 4. player_elo_ratings — exact zero-sum pairwise math
    # ------------------------------------------------------------------
    conn2 = fresh_conn()
    gid1, err = w.create_game(
        conn2, date="2026-01-01", note=None,
        participants=[
            {"deck_name": "A's Deck", "deck_id": None, "is_winner": True, "player_name": "A"},
            {"deck_name": "B's Deck", "deck_id": None, "is_winner": False, "player_name": "B"},
            {"deck_name": "C's Deck", "deck_id": None, "is_winner": False, "player_name": "C"},
            {"deck_name": "D's Deck", "deck_id": None, "is_winner": False, "player_name": "D"},
        ],
    )
    check("sanity: the 4-player game logged fine", err is None)

    elo = q.player_elo_ratings(conn2)
    ratings = dict(zip(elo["player_name"], elo["rating"]))
    # All 4 start at 1000. A beats B, C, D pairwise (+0.5*k each vs. each);
    # B/C/D pairwise are draws (no change). k=20 default -> A: 1000+30=1030,
    # B=C=D: 1000-10=990. See queries.player_elo_ratings()'s docstring for
    # the derivation.
    check("winner A's rating is exactly 1030 after one 4-player game", ratings["A"] == 1030)
    check("loser B's rating is exactly 990", ratings["B"] == 990)
    check("loser C's rating is exactly 990", ratings["C"] == 990)
    check("loser D's rating is exactly 990", ratings["D"] == 990)
    check(
        "zero-sum invariant: total rating change across all 4 players is 0 (every pairwise update is zero-sum)",
        sum(ratings.values()) == 4000,
    )
    games_played = dict(zip(elo["player_name"], elo["games_played"]))
    check("every player is credited with 1 game_played", all(v == 1 for v in games_played.values()))

    # A solo-named game (only 1 player_name recorded) shouldn't move anyone.
    w.create_game(
        conn2, date="2026-01-02", note=None,
        participants=[
            {"deck_name": "A's Deck", "deck_id": None, "is_winner": True, "player_name": "A"},
            {"deck_name": "Unnamed opponent", "deck_id": None, "is_winner": False, "player_name": None},
        ],
    )
    elo2 = q.player_elo_ratings(conn2)
    ratings2 = dict(zip(elo2["player_name"], elo2["rating"]))
    check("a game with only 1 named player doesn't change any rating", ratings2["A"] == 1030)
    check("and doesn't add a phantom player", set(ratings2.keys()) == {"A", "B", "C", "D"})

    empty_elo = q.player_elo_ratings(fresh_conn())
    check("player_elo_ratings on an empty DB returns an empty DataFrame with the right columns", empty_elo.empty and list(empty_elo.columns) == ["player_name", "rating", "games_played"])

    # ------------------------------------------------------------------
    # 5. player_head_to_head_matrix
    # ------------------------------------------------------------------
    h2h = q.player_head_to_head_matrix(conn2)
    check("head-to-head matrix is square, indexed/columned by all 4 players", set(h2h.index) == set(h2h.columns) == {"A", "B", "C", "D"})
    check("diagonal is NaN", h2h.loc["A", "A"] != h2h.loc["A", "A"])  # NaN != NaN
    check("A beat B in their one shared game -> A's row vs B is 1.0", h2h.loc["A", "B"] == 1.0)
    check("B lost to A in their one shared game -> B's row vs A is 0.0", h2h.loc["B", "A"] == 0.0)
    check("B and C shared a game with neither winning -> 0.0 for both directions", h2h.loc["B", "C"] == 0.0 and h2h.loc["C", "B"] == 0.0)

    empty_h2h = q.player_head_to_head_matrix(fresh_conn())
    check("player_head_to_head_matrix on an empty DB returns an empty DataFrame", empty_h2h.empty)

    conn.close()
    conn2.close()

    print("\nAll Prompt Pass 7 offline checks passed.")


if __name__ == "__main__":
    main()

"""Commander game tracking: the Add Game form's pure logic
(game_form.resolve_seat / validate_and_build_participants), the
create_game / update_game write rules, what deleting a tracked deck does to
its logged games, and the player stats (distinct names, ELO, head-to-head).

Replaces the old scripts/_test_prompt_pass7 harness.
"""
import pytest

from dashboard_lib import game_form as gf
from dashboard_lib import queries as q
from dashboard_lib import writes as w

FT, NEW = gf.FREE_TEXT_OPTION, gf.NEW_ENTRY_OPTION
LABEL_TO_ID = {"My Deck": 1, "Their Deck": 2}
BLANK_SEAT = (FT, NEW, "", NEW, "", False)


def seat(deck_name, deck_id, winner, player):
    return {"deck_name": deck_name, "deck_id": deck_id, "is_winner": winner, "player_name": player}


# ------------------------------------------------------------------
# resolve_seat: a seat is (tracked-deck pick, known-opponent-deck pick,
# free-text deck, known-player pick, free-text player, won)
# ------------------------------------------------------------------
def test_blank_seat_is_unused():
    deck_name, _, _, used = gf.resolve_seat(FT, NEW, "", NEW, "", False, LABEL_TO_ID)
    assert used is False and deck_name is None


def test_tracked_deck_pick_wins_over_everything():
    deck_name, deck_id, _, used = gf.resolve_seat("My Deck", NEW, "ignored", NEW, "", False, LABEL_TO_ID)
    assert (deck_name, deck_id, used) == ("My Deck", 1, True)


def test_known_opponent_deck_has_no_deck_id():
    deck_name, deck_id, _, _ = gf.resolve_seat(FT, "Rakdos Showstopper", "", NEW, "", False, LABEL_TO_ID)
    assert (deck_name, deck_id) == ("Rakdos Showstopper", None)


def test_free_text_deck_is_the_last_resort_and_stripped():
    deck_name, deck_id, _, _ = gf.resolve_seat(FT, NEW, "  Brand New Opponent Deck  ", NEW, "", False, LABEL_TO_ID)
    assert (deck_name, deck_id) == ("Brand New Opponent Deck", None)


def test_known_player_beats_free_text_player():
    assert gf.resolve_seat(FT, NEW, "", "Anthony", "ignored", False, LABEL_TO_ID)[2] == "Anthony"
    assert gf.resolve_seat(FT, NEW, "", NEW, "  Brand New Player  ", False, LABEL_TO_ID)[2] == "Brand New Player"


def test_seat_with_a_player_and_win_but_no_deck_still_counts_as_used():
    # validate_and_build_participants turns that into a rejection; resolve_seat
    # must not silently treat the seat as blank.
    deck_name, _, _, used = gf.resolve_seat(FT, NEW, "", "Anthony", "", True, LABEL_TO_ID)
    assert used is True and deck_name is None


# ------------------------------------------------------------------
# validate_and_build_participants
# ------------------------------------------------------------------
def test_half_filled_seat_is_rejected_with_its_seat_number():
    seats = [(FT, NEW, "", "Anthony", "", True), BLANK_SEAT, BLANK_SEAT, BLANK_SEAT]
    participants, err = gf.validate_and_build_participants(seats, LABEL_TO_ID)
    assert participants is None and "Seat 1" in err


def test_valid_two_seat_submission_builds_participants():
    seats = [("My Deck", NEW, "", "Me", "", True),
             (FT, NEW, "Free Text Opponent", "Anthony", "", False), BLANK_SEAT, BLANK_SEAT]
    participants, err = gf.validate_and_build_participants(seats, LABEL_TO_ID)
    assert err is None
    assert participants == [seat("My Deck", 1, True, "Me"),
                            seat("Free Text Opponent", None, False, "Anthony")]


def test_all_blank_submission_is_rejected():
    participants, err = gf.validate_and_build_participants([BLANK_SEAT] * 4, LABEL_TO_ID)
    assert participants is None and "at least one" in err.lower()


@pytest.mark.parametrize("first_won, second_won, message", [
    (False, False, "winner"),       # nobody won
    (True, True, "one seat"),       # two winners
])
def test_exactly_one_winner_is_required(first_won, second_won, message):
    seats = [("My Deck", NEW, "", "Me", "", first_won),
             (FT, NEW, "Free Text Opponent", "Anthony", "", second_won), BLANK_SEAT, BLANK_SEAT]
    participants, err = gf.validate_and_build_participants(seats, LABEL_TO_ID)
    assert participants is None and message in err.lower()


# ------------------------------------------------------------------
# create_game / update_game
# ------------------------------------------------------------------
@pytest.fixture
def two_decks(conn):
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'My Deck')")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (2, 'Their Deck')")
    conn.commit()
    return conn


def duel(my_won, their_won):
    return [seat("My Deck", 1, my_won, "Me"), seat("Their Deck", 2, their_won, "Anthony")]


@pytest.mark.parametrize("participants", [duel(False, False), duel(True, True), []])
def test_create_game_rejects_anything_but_exactly_one_winner(two_decks, participants):
    game_id, err = w.create_game(two_decks, date="2026-01-01", note=None, participants=participants)
    assert game_id is None and err is not None
    assert two_decks.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0


def test_create_game_numbers_seats_by_list_position(two_decks):
    game_id, err = w.create_game(two_decks, date="2026-01-01", note="first game", participants=duel(True, False))
    assert err is None
    seats = two_decks.execute("SELECT seat FROM game_participants WHERE game_id=? ORDER BY seat",
                              (game_id,)).fetchall()
    assert [s["seat"] for s in seats] == [1, 2]


@pytest.fixture
def logged_game(two_decks):
    game_id, _ = w.create_game(two_decks, date="2026-01-01", note="first game", participants=duel(True, False))
    return two_decks, game_id


def test_update_game_replaces_date_note_and_participants(logged_game):
    conn, game_id = logged_game
    ok, err = w.update_game(conn, game_id, date="2026-01-02", note="corrected", participants=[
        seat("My Deck", 1, False, "Me"), seat("Their Deck", 2, True, "Anthony"),
        seat("Some Free Text Deck", None, False, "Joe G")])
    assert ok is True and err is None
    game = conn.execute("SELECT date, note FROM games WHERE game_id=?", (game_id,)).fetchone()
    assert (game["date"], game["note"]) == ("2026-01-02", "corrected")
    seats = conn.execute("SELECT seat, deck_id, is_winner, player_name FROM game_participants "
                         "WHERE game_id=? ORDER BY seat", (game_id,)).fetchall()
    assert [s["seat"] for s in seats] == [1, 2, 3]                 # 2 seats replaced by 3
    assert seats[1]["is_winner"] == 1 and seats[1]["player_name"] == "Anthony"
    assert seats[2]["deck_id"] is None                              # free-text seat is untracked


def test_update_game_applies_the_same_winner_rule(logged_game):
    conn, game_id = logged_game
    ok, err = w.update_game(conn, game_id, date="2026-01-02", note=None,
                            participants=[seat("My Deck", 1, False, "Me")])
    assert ok is False and err is not None


def test_update_game_on_a_missing_game(two_decks):
    assert w.update_game(two_decks, 99999, date="2026-01-02", note=None, participants=[]) == (False, "Game not found.")


# ------------------------------------------------------------------
# Deleting a tracked deck detaches its seats but keeps the game
# ------------------------------------------------------------------
def test_deleting_a_tracked_deck_detaches_its_seat(logged_game):
    conn, game_id = logged_game
    name, err, _redirected, _dropped = w.delete_deck(conn, 1)
    assert (name, err) == ("My Deck", None)
    assert conn.execute("SELECT 1 FROM decks WHERE deck_id=1").fetchone() is None
    assert conn.execute("SELECT 1 FROM games WHERE game_id=?", (game_id,)).fetchone() is not None

    participants = {p["deck_name"]: p for p in q.game_detail(conn, game_id)["participants"]}
    assert participants["My Deck"]["deck_id"] is None               # name text survives, link is gone
    assert conn.execute("SELECT is_own_deck FROM game_participants WHERE game_id=? AND deck_name='My Deck'",
                        (game_id,)).fetchone()[0] == 0
    assert participants["Their Deck"]["deck_id"] == 2 and participants["Their Deck"]["is_winner"] is False


def test_distinct_names_include_detached_decks(logged_game):
    conn, game_id = logged_game
    w.update_game(conn, game_id, date="2026-01-02", note=None, participants=[
        seat("My Deck", 1, False, "Me"), seat("Their Deck", 2, True, "Anthony"),
        seat("Some Free Text Deck", None, False, "Joe G")])
    w.delete_deck(conn, 1)
    players = q.distinct_player_names(conn)
    assert set(players) == {"Me", "Anthony", "Joe G"}
    assert players == sorted(players, key=str.lower)
    # Both the always-free-text deck and the now-detached former tracked one.
    assert set(q.distinct_untracked_deck_names(conn)) == {"Some Free Text Deck", "My Deck"}


# ------------------------------------------------------------------
# player_elo_ratings / player_head_to_head_matrix
# ------------------------------------------------------------------
@pytest.fixture
def four_player_game(conn):
    w.create_game(conn, date="2026-01-01", note=None, participants=[
        seat("A's Deck", None, True, "A"), seat("B's Deck", None, False, "B"),
        seat("C's Deck", None, False, "C"), seat("D's Deck", None, False, "D")])
    return conn


def test_elo_after_one_four_player_game(four_player_game):
    elo = q.player_elo_ratings(four_player_game)
    ratings = dict(zip(elo["player_name"], elo["rating"]))
    # Everyone starts at 1000. A beats B, C and D pairwise (+10 each at the
    # default k=20, so +30); the losers draw each other, so each drops 10.
    assert ratings == {"A": 1030, "B": 990, "C": 990, "D": 990}
    assert sum(ratings.values()) == 4000                      # every pairwise update is zero-sum
    assert set(elo["games_played"]) == {1}


def test_a_game_with_one_named_player_moves_nobody(four_player_game):
    w.create_game(four_player_game, date="2026-01-02", note=None, participants=[
        seat("A's Deck", None, True, "A"), seat("Unnamed opponent", None, False, None)])
    elo = q.player_elo_ratings(four_player_game)
    ratings = dict(zip(elo["player_name"], elo["rating"]))
    assert ratings["A"] == 1030 and set(ratings) == {"A", "B", "C", "D"}


def test_elo_on_an_empty_database(conn):
    elo = q.player_elo_ratings(conn)
    assert elo.empty and list(elo.columns) == ["player_name", "rating", "games_played"]


def test_head_to_head_matrix(four_player_game):
    h2h = q.player_head_to_head_matrix(four_player_game)
    assert set(h2h.index) == set(h2h.columns) == {"A", "B", "C", "D"}
    assert h2h.loc["A", "A"] != h2h.loc["A", "A"]             # the diagonal is NaN
    assert h2h.loc["A", "B"] == 1.0                           # A beat B
    assert h2h.loc["B", "A"] == 0.0
    assert h2h.loc["B", "C"] == 0.0 == h2h.loc["C", "B"]      # shared a game, neither won


def test_head_to_head_on_an_empty_database(conn):
    assert q.player_head_to_head_matrix(conn).empty

"""
Pure resolution/validation logic for the Commander Game Tracking page's
"log a new game" and "edit a game" forms (Prompt Pass 7). No Streamlit
dependency — same "dashboard_lib split" pattern as formatting.py/
probability.py/writes.py, so this can (and is) unit-tested directly
against plain tuples/dicts rather than only being exercised by clicking
through the real page.

The page itself owns all the actual st.* widget calls; it hands this
module the RAW widget values for one seat (or a whole form's worth of
seats) and gets back resolved, validated data.
"""

# Sentinel option values — must match exactly what the page puts at the
# front of each selectbox's options list.
FREE_TEXT_OPTION = "(free text below)"
NEW_ENTRY_OPTION = "+ new / other (type below)"


def resolve_seat(deck_pick, opp_pick, opp_free, player_pick, player_free, is_winner, label_to_id):
    """Resolves one seat's raw widget values into
    (deck_name, deck_id, player_name, used).

    deck_pick: the "Tracked deck" selectbox value (FREE_TEXT_OPTION or
      a deck name from `label_to_id`).
    opp_pick: the "known opponent deck" selectbox value (NEW_ENTRY_OPTION
      or a previously-logged untracked deck name).
    opp_free: the free-text "type a new opponent deck name" input.
    player_pick: the "Player" selectbox value (NEW_ENTRY_OPTION or a
      previously-logged player name).
    player_free: the free-text "type a new player name" input.
    is_winner: the seat's "Won" checkbox value.
    label_to_id: {deck_name: deck_id} for every currently tracked deck.

    Precedence, deck: tracked-deck dropdown > known-opponent dropdown >
    free-text opponent name > (nothing entered). Precedence, player:
    known-player dropdown > free-text player name > (nothing entered,
    which is fine — player name has always been optional).

    'used' is True the moment the person has entered ANYTHING for this
    seat (a deck, a player, "Won", or raw text even if a dropdown
    sentinel ends up taking precedence over it) — so a fully-untouched
    seat can be silently skipped by the caller, while a half-filled one
    (e.g. a player name typed in but no deck ever chosen) is left for
    validate_and_build_participants() to flag rather than silently
    losing data, which is what the pre-Prompt-Pass-7 page used to do."""
    if deck_pick != FREE_TEXT_OPTION:
        deck_name, deck_id = deck_pick, int(label_to_id[deck_pick])
    elif opp_pick != NEW_ENTRY_OPTION and opp_pick:
        deck_name, deck_id = opp_pick, None
    elif opp_free.strip():
        deck_name, deck_id = opp_free.strip(), None
    else:
        deck_name, deck_id = None, None

    if player_pick != NEW_ENTRY_OPTION and player_pick:
        player_name = player_pick
    elif player_free.strip():
        player_name = player_free.strip()
    else:
        player_name = None

    used = bool(
        deck_name or player_name or is_winner
        or opp_free.strip() or player_free.strip()
        or deck_pick != FREE_TEXT_OPTION
        or opp_pick != NEW_ENTRY_OPTION
        or player_pick != NEW_ENTRY_OPTION
    )
    return deck_name, deck_id, player_name, used


def validate_and_build_participants(seat_raw_widgets, label_to_id):
    """Shared by the "log a new game" form and the "edit a game" form:
    resolves every seat via resolve_seat(), then enforces the Prompt
    Pass 7 submission rules —

      1. Every seat with any info at all must resolve to a real deck
         (a seat with e.g. a player name and "Won" checked but no deck
         picked is rejected rather than silently dropped).
      2. Among seats that DO have a deck, at least one participant is
         required overall.
      3. Exactly one seat must be marked the winner — no more, no
         fewer (a 0-winner "draw" is no longer accepted from this
         form; see writes._validate_game_participants()'s docstring
         for how this interacts with older, migrate.py-imported draws,
         which bypass this validation entirely and are unaffected).

    seat_raw_widgets: list of (deck_pick, opp_pick, opp_free,
    player_pick, player_free, is_winner) tuples, one per seat, in seat
    order. Returns (participants, error_message) — error_message is
    None on success, in which case `participants` is ready to pass
    straight to writes.create_game()/update_game()."""
    participants = []
    incomplete_seats = []
    winner_count = 0
    for i, seat_vals in enumerate(seat_raw_widgets, start=1):
        deck_name, deck_id, player_name, used = resolve_seat(*seat_vals, label_to_id)
        is_winner = seat_vals[-1]
        if not used:
            continue
        if deck_name is None:
            incomplete_seats.append(i)
            continue
        if is_winner:
            winner_count += 1
        participants.append(
            {"deck_name": deck_name, "deck_id": deck_id, "is_winner": is_winner, "player_name": player_name}
        )

    if incomplete_seats:
        seats = ", ".join(str(s) for s in incomplete_seats)
        return None, (
            f"Seat {seats} has other info filled in but no deck selected \u2014 pick a tracked "
            "deck, choose a known opponent deck, or type a new opponent deck name."
        )
    if not participants:
        return None, "Add at least one deck/participant before logging the game."
    if winner_count == 0:
        return None, "Select a winner before logging the game (check \u201cWon\u201d for exactly one seat)."
    if winner_count > 1:
        return None, "More than one seat is marked as the winner \u2014 pick at most one."
    return participants, None


def format_game_option(gid, games_df):
    """Display label for a game in the Edit/Delete pickers — date plus
    the same rendered participant strings the game-history list already
    uses. Deliberately carries no "Game #"/game_id text (Prompt Pass 7)
    — the game_id is still the underlying selectbox VALUE (so two games
    with an identical label are still distinguishable to Streamlit),
    it's just never shown."""
    row = games_df.loc[games_df["game_id"] == gid].iloc[0]
    date_str = row["date"] or "Unknown date"
    parts_str = ", ".join(row["participants"]) or "no participants"
    return f"{date_str} \u2014 {parts_str}"

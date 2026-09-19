"""
Commander Game Tracking page (Phase 2; edit/autofill/tabs/ELO added in
Prompt Pass 7).

Log Commander games — up to 4 seats per game, each either a tracked deck
(picked from a dropdown) or an opponent's deck that isn't in the library
(picked from an autofill dropdown of previously-logged names, or typed
fresh) — with an optional player name (same autofill-or-type pattern)
and win flag per seat, plus a notes field. An existing game can also be
edited in place, not just deleted and re-logged from scratch.

Two tabs:
  - "Log & manage games" — the log-a-new-game form, the rendered game
    history, and Edit/Delete expanders.
  - "Player & deck stats" — win-rate-by-deck/by-player tables, plus a
    Prompt-Pass-7 ELO rating table and head-to-head win-rate matrix —
    moved into its own tab so long game logs don't push these metrics
    off the bottom of the page.

Player name is a field as of Phase 2 (game_participants.player_name).
Games logged before this existed won't have one, so they're simply
omitted from anything scoped to player_name (win rate, ELO, head-to-head)
rather than shown with a blank player.

No Game # is tracked or shown anywhere on this page (Prompt Pass 7) —
game_id is an internal primary key only, never surfaced as a sequence a
person is meant to read meaning into; entries are always ordered by date
(then game_id as a same-day tiebreaker) instead, so out-of-order data
entry never produces a misleading "Game #".

No CSV editing or re-migration required for any of this — same as the
rest of the dashboard since Prompt Pass 1.
"""
import sys
import os
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from dashboard_lib import db, loaders, writes, queries as q, game_form as gf

st.set_page_config(page_title="Commander Game Tracking · MTG Dashboard", page_icon="🏆", layout="wide")

db.require_db()
conn = db.get_connection()

st.title("🏆 Commander Game Tracking")

st.sidebar.header("Game Tracking")
db.refresh_data_button()

decks_df = loaders.load_decks_df(conn)
deck_names = list(decks_df["name"]) if not decks_df.empty else []
label_to_id = dict(zip(decks_df["name"], decks_df["deck_id"])) if not decks_df.empty else {}

known_players = loaders.load_known_player_names(conn)
known_opponent_decks = loaders.load_known_untracked_deck_names(conn)

FREE_TEXT_OPTION = gf.FREE_TEXT_OPTION
NEW_ENTRY_OPTION = gf.NEW_ENTRY_OPTION


def _render_seat_widgets(key_prefix, seat_num, deck_index=0, opp_default="", player_default="", winner_default=False):
    """Renders one seat's row of widgets (two rows: deck info, then
    player info) and returns the raw tuple game_form.resolve_seat() expects.
    Shared by the "log new" and "edit" forms so both stay in sync."""
    st.markdown(f"**Seat {seat_num}**")
    c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
    deck_pick = c1.selectbox(
        "Tracked deck", [FREE_TEXT_OPTION] + deck_names, index=deck_index,
        key=f"{key_prefix}_seat{seat_num}_deckpick",
    )
    opp_pick = c2.selectbox(
        "Or a known opponent deck", [NEW_ENTRY_OPTION] + known_opponent_decks,
        key=f"{key_prefix}_seat{seat_num}_opppick",
    )
    opp_free = c3.text_input(
        "Or type a new opponent deck name", value=opp_default,
        key=f"{key_prefix}_seat{seat_num}_freename",
    )
    is_winner = c4.checkbox("Won", value=winner_default, key=f"{key_prefix}_seat{seat_num}_winner")

    p1, p2 = st.columns([2, 2])
    player_pick = p1.selectbox(
        "Player", [NEW_ENTRY_OPTION] + known_players, key=f"{key_prefix}_seat{seat_num}_playerpick",
    )
    player_free = p2.text_input(
        "Or type a new player name (optional)", value=player_default,
        key=f"{key_prefix}_seat{seat_num}_player",
    )
    return (deck_pick, opp_pick, opp_free, player_pick, player_free, is_winner)


tab_log, tab_stats = st.tabs(["\U0001F4DD Log & manage games", "\U0001F4CA Player & deck stats"])

# ------------------------------------------------------------------
# Tab 1: log a new game, game history, edit, delete
# ------------------------------------------------------------------
with tab_log:
    st.subheader("Log a new game")
    st.caption(
        "For each seat, either pick one of your tracked decks, or pick a known opponent deck "
        "you've logged before, or type a new one. Player names work the same way. Leave a whole "
        "seat untouched to skip it (e.g. a 3-player pod)."
    )

    with st.form("gt_log_game_form", clear_on_submit=True):
        game_date = st.date_input("Date", value=datetime.date.today(), key="gt_new_date")

        seat_widgets = [_render_seat_widgets("gt_new", i) for i in range(1, 5)]

        note = st.text_area("Notes (optional)", key="gt_new_note")
        submitted = st.form_submit_button("📝 Log game")

    if submitted:
        participants, val_err = gf.validate_and_build_participants(seat_widgets, label_to_id)
        if val_err:
            st.error(val_err)
        else:
            game_id, err = writes.create_game(
                conn, date=game_date.isoformat(), note=note.strip() or None, participants=participants
            )
            if err:
                st.error(err)
            else:
                loaders.invalidate_game_tracking_caches()
                st.success("Game logged.")
                st.rerun()

    st.divider()

    # ------------------------------------------------------------------
    # Game history — rendered, not a raw dump
    # ------------------------------------------------------------------
    st.subheader("Game history")

    games_df = loaders.load_games_list(conn)
    if games_df.empty:
        st.info("No games logged yet — use the form above to log your first one.")
    else:
        for _, g in games_df.iterrows():
            with st.container(border=True):
                st.markdown(f"**{g['date'] or 'Unknown date'}**")
                for line in g["participants"]:
                    st.write(f"- {line}")
                if pd.notna(g.get("note")) and g["note"]:
                    st.caption(g["note"])

        game_ids = games_df["game_id"].tolist()

        with st.expander("✏️ Edit a logged game"):
            st.caption("Pick a game to load it into an editable form below, then save your changes.")
            edit_gid = st.selectbox(
                "Pick a game", game_ids,
                format_func=lambda gid: gf.format_game_option(gid, games_df),
                key="gt_edit_choice",
            )
            detail = q.game_detail(conn, edit_gid)
            if detail is None:
                st.info("That game no longer exists — pick another.")
            else:
                existing_by_seat = {p["seat"]: p for p in detail["participants"]}
                key_prefix = f"gt_edit_{edit_gid}"  # keying by game_id forces fresh
                # defaults whenever a different game is selected, since
                # Streamlit widgets otherwise keep whatever value they
                # last held under a given key rather than re-reading
                # `value=`/`index=` on every rerun.

                with st.form(f"{key_prefix}_form"):
                    default_date = (
                        datetime.date.fromisoformat(detail["date"]) if detail["date"] else datetime.date.today()
                    )
                    edit_date = st.date_input("Date", value=default_date, key=f"{key_prefix}_date")

                    edit_seat_widgets = []
                    for i in range(1, 5):
                        existing = existing_by_seat.get(i)
                        if existing and existing["deck_id"] is not None and existing["deck_name"] in deck_names:
                            deck_index = ([FREE_TEXT_OPTION] + deck_names).index(existing["deck_name"])
                        else:
                            deck_index = 0
                        opp_default = existing["deck_name"] if existing and existing["deck_id"] is None else ""
                        player_default = (existing.get("player_name") or "") if existing else ""
                        winner_default = bool(existing["is_winner"]) if existing else False
                        edit_seat_widgets.append(
                            _render_seat_widgets(
                                key_prefix, i, deck_index=deck_index, opp_default=opp_default,
                                player_default=player_default, winner_default=winner_default,
                            )
                        )

                    edit_note = st.text_area(
                        "Notes (optional)", value=detail.get("note") or "", key=f"{key_prefix}_note"
                    )
                    save_clicked = st.form_submit_button("💾 Save changes")

                if save_clicked:
                    participants, val_err = gf.validate_and_build_participants(edit_seat_widgets, label_to_id)
                    if val_err:
                        st.error(val_err)
                    else:
                        ok, err = writes.update_game(
                            conn, edit_gid, date=edit_date.isoformat(),
                            note=edit_note.strip() or None, participants=participants,
                        )
                        if err:
                            st.error(err)
                        else:
                            loaders.invalidate_game_tracking_caches()
                            st.success("Game updated.")
                            st.rerun()

        with st.expander("🗑️ Delete a logged game"):
            st.caption("For correcting a mis-entered log — not part of normal play.")
            del_gid = st.selectbox(
                "Pick a game", game_ids,
                format_func=lambda gid: gf.format_game_option(gid, games_df),
                key="gt_delete_choice",
            )
            if st.button("🗑️ Delete this game", key="gt_delete_btn"):
                deleted, err = writes.delete_game(conn, del_gid)
                if err:
                    st.error(err)
                else:
                    loaders.invalidate_game_tracking_caches()
                    st.success("Game deleted.")
                    st.rerun()

# ------------------------------------------------------------------
# Tab 2: win rate comparisons, ELO, head-to-head (Prompt Pass 7 moved
# this whole section into its own tab so a long game history doesn't
# push it off the bottom of the page)
# ------------------------------------------------------------------
with tab_stats:
    st.subheader("Win rate comparisons")

    wr_col1, wr_col2 = st.columns(2)

    with wr_col1:
        st.markdown("**By deck**")
        deck_wr = loaders.load_deck_win_rates(conn)
        if deck_wr.empty:
            st.caption("No games logged for any tracked deck yet.")
        else:
            deck_wr = deck_wr.copy()
            deck_wr["Win %"] = deck_wr["win_rate"].apply(lambda v: f"{v * 100:.0f}%" if pd.notna(v) else "—")
            st.dataframe(
                deck_wr.rename(
                    columns={"name": "Deck", "games_played": "Played", "wins": "Wins", "losses": "Losses"}
                )[["Deck", "Played", "Wins", "Losses", "Win %"]],
                hide_index=True, use_container_width=True,
            )

    with wr_col2:
        st.markdown("**By player**")
        player_wr = loaders.load_player_win_rates(conn)
        if player_wr.empty:
            st.caption("No games with a player name recorded yet.")
        else:
            player_wr = player_wr.copy()
            player_wr["Win %"] = player_wr["win_rate"].apply(lambda v: f"{v * 100:.0f}%" if pd.notna(v) else "—")
            st.dataframe(
                player_wr.rename(
                    columns={"player_name": "Player", "games_played": "Played", "wins": "Wins", "losses": "Losses"}
                )[["Player", "Played", "Wins", "Losses", "Win %"]],
                hide_index=True, use_container_width=True,
            )

    st.divider()

    st.subheader("Player ELO ratings")
    st.caption(
        "A multiplayer ELO variant, not a straight 1v1 ladder: a game's winner is scored a win "
        "against every other seated player, and every pair of non-winners is scored a draw "
        "against each other, since a 4-player pod's baseline win rate isn't 50/50 the way a 1v1 "
        "match is. Only games with 2+ recorded player names count (same scoping as \u201cBy "
        "player\u201d above)."
    )
    elo_df = loaders.load_player_elo_ratings(conn)
    if elo_df.empty:
        st.caption("No games with 2+ recorded player names yet.")
    else:
        st.dataframe(
            elo_df.rename(columns={"player_name": "Player", "rating": "Rating", "games_played": "Games"}),
            hide_index=True, use_container_width=True,
        )

    st.divider()

    st.subheader("Head-to-head win rates")
    st.caption(
        "Row player's win rate against column player, counting only games the two of them "
        "shared (with anyone else also at the table). \u2014 means they've never shared a "
        "logged game."
    )
    h2h_df = loaders.load_player_head_to_head(conn)
    if h2h_df.empty:
        st.caption("Not enough shared games with recorded player names yet.")
    else:
        display_h2h = h2h_df.copy()
        for col in display_h2h.columns:
            display_h2h[col] = display_h2h[col].apply(lambda v: f"{v * 100:.0f}%" if pd.notna(v) else "—")
        st.dataframe(display_h2h, use_container_width=True)

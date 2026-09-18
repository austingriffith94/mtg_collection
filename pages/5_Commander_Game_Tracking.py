"""
Commander Game Tracking page (Phase 2).

Log Commander games — up to 4 seats per game, each either a tracked deck
(picked from a dropdown) or a free-text entry for an opponent's deck
that isn't in the library — with an optional player name and win flag
per seat, plus a notes field. Below that: an actual rendered game
history (not a raw table dump), and win-rate summaries by deck and by
player.

Player name is a new field as of Phase 2 (game_participants.player_name).
Games logged before this existed won't have one, so they're simply
omitted from the "Win rate by player" summary rather than shown with a
blank player.

No CSV editing or re-migration required for any of this — same as the
rest of the dashboard since Prompt Pass 1.
"""
import sys
import os
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from dashboard_lib import db, loaders, writes

st.set_page_config(page_title="Commander Game Tracking · MTG Dashboard", page_icon="🏆", layout="wide")

db.require_db()
conn = db.get_connection()

st.title("🏆 Commander Game Tracking")

st.sidebar.header("Game Tracking")
db.refresh_data_button()

decks_df = loaders.load_decks_df(conn)
deck_names = list(decks_df["name"]) if not decks_df.empty else []

# ------------------------------------------------------------------
# Log a new game
# ------------------------------------------------------------------
st.subheader("Log a new game")
st.caption(
    "For each seat, either pick one of your tracked decks from the dropdown, or leave it as "
    "'(free text)' and type an opponent's deck name instead. Leave a whole seat blank to skip it."
)

FREE_TEXT_OPTION = "(free text below)"

with st.form("gt_log_game_form", clear_on_submit=True):
    game_date = st.date_input("Date", value=datetime.date.today(), key="gt_new_date")

    seat_widgets = []
    for i in range(1, 5):
        st.markdown(f"**Seat {i}**")
        c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
        deck_pick = c1.selectbox(
            "Tracked deck", [FREE_TEXT_OPTION] + deck_names, key=f"gt_seat{i}_deckpick"
        )
        free_name = c2.text_input("Or opponent's deck name", key=f"gt_seat{i}_freename")
        player_name = c3.text_input("Player (optional)", key=f"gt_seat{i}_player")
        is_winner = c4.checkbox("Won", key=f"gt_seat{i}_winner")
        seat_widgets.append((deck_pick, free_name, player_name, is_winner))

    note = st.text_area("Notes (optional)", key="gt_new_note")
    submitted = st.form_submit_button("📝 Log game")

if submitted:
    label_to_id = dict(zip(decks_df["name"], decks_df["deck_id"])) if not decks_df.empty else {}
    participants = []
    winner_count = 0
    for deck_pick, free_name, player_name, is_winner in seat_widgets:
        if deck_pick != FREE_TEXT_OPTION:
            deck_name, deck_id = deck_pick, int(label_to_id[deck_pick])
        elif free_name.strip():
            deck_name, deck_id = free_name.strip(), None
        else:
            continue
        if is_winner:
            winner_count += 1
        participants.append(
            {
                "deck_name": deck_name,
                "deck_id": deck_id,
                "is_winner": is_winner,
                "player_name": player_name.strip() or None,
            }
        )

    if winner_count > 1:
        st.error("More than one seat is marked as the winner — pick at most one (or none, for a draw).")
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

    with st.expander("🗑️ Delete a logged game"):
        st.caption("For correcting a mis-entered log — not part of normal play.")
        game_options = {
            f"#{row['game_id']} — {row['date'] or 'Unknown date'} — {', '.join(row['participants']) or 'no participants'}": row["game_id"]
            for _, row in games_df.iterrows()
        }
        game_choice = st.selectbox("Pick a game", list(game_options.keys()), key="gt_delete_choice")
        if st.button("🗑️ Delete this game", key="gt_delete_btn"):
            deleted, err = writes.delete_game(conn, game_options[game_choice])
            if err:
                st.error(err)
            else:
                loaders.invalidate_game_tracking_caches()
                st.success("Game deleted.")
                st.rerun()

st.divider()

# ------------------------------------------------------------------
# Win rate comparisons
# ------------------------------------------------------------------
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

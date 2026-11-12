import os

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

st.set_page_config(page_title="Live match", page_icon="📡", layout="wide")
st.title("Live match")

engine = create_engine(os.environ["DATABASE_URL"])


def query(sql, **params):
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


@st.fragment(run_every=2)          # the WHOLE view re-runs every 2 s, so a replay that starts later shows up by itself
def live_page():
    matches = query("""
        SELECT s.match_id, m.match_date, th.team_name AS home, ta.team_name AS away,
               max(s.updated_at) AS last_update
        FROM live.match_state s
        JOIN dw.dim_match m  ON m.statsbomb_id = s.match_id
        JOIN dw.dim_team  th ON th.team_key = m.home_team_key
        JOIN dw.dim_team  ta ON ta.team_key = m.away_team_key
        GROUP BY s.match_id, m.match_date, th.team_name, ta.team_name
        ORDER BY max(s.updated_at) DESC
    """)

    if matches.empty:
        st.info(
            "No live data yet. This page checks again every 2 seconds. Start a replay from the repo folder:\n\n"
            "`docker compose run --rm replay --list`\n\n"
            "`docker compose run --rm replay --match-id <id> --speed 60`"
        )
        return

    labels = {r.match_id: f"{r.home} vs {r.away}  ({r.match_date})" for r in matches.itertuples()}
    follow = st.checkbox("Follow the most recently updated match", value=True, key="follow_latest")
    if follow:
        match_id = matches.iloc[0].match_id
        st.caption(f"Showing: {labels[match_id]}")
    else:
        match_id = st.selectbox("Match", list(labels), format_func=labels.get, key="match_pick")
    row = matches[matches.match_id == match_id].iloc[0]
    home, away = row.home, row.away

    state = query("SELECT * FROM live.match_state WHERE match_id = :m ORDER BY minute DESC LIMIT 1", m=int(match_id))
    if state.empty:
        st.info("Waiting for the first events...")
        return
    s = state.iloc[0]

    st.markdown(
        f"<h1 style='text-align:center;margin-bottom:0'>{home} {int(s.home_goals)} – {int(s.away_goals)} {away}</h1>"
        f"<p style='text-align:center;opacity:.7'>minute {int(s.minute)}</p>",
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"xG  {home} / {away}", f"{float(s.home_xg):.2f} / {float(s.away_xg):.2f}")
    c2.metric("Shots", f"{int(s.home_shots)} / {int(s.away_shots)}")
    poss = s.home_possession
    c3.metric("Possession (pass share)", "–" if pd.isna(poss) else f"{float(poss):.0f}% / {100 - float(poss):.0f}%")
    c4.metric("Pressures", f"{int(s.home_pressures)} / {int(s.away_pressures)}")

    series = query("SELECT minute, home_xg, away_xg, momentum FROM live.match_state "
                   "WHERE match_id = :m ORDER BY minute", m=int(match_id)).set_index("minute")
    left, right = st.columns(2)
    with left:
        st.subheader("Cumulative xG")
        st.line_chart(series[["home_xg", "away_xg"]].astype(float).rename(columns={"home_xg": home, "away_xg": away}))
    with right:
        st.subheader("Momentum")
        st.caption(f"Above 0: {home} on top. Below 0: {away} on top. Last 10 minutes of play.")
        st.line_chart(series[["momentum"]].astype(float))

    st.subheader("Key events")
    feed = query("""
        SELECT minute, second, event_type AS event, team, player,
               detail->>'xg' AS xg, detail->>'card' AS card, detail->>'on' AS replaced_by
        FROM live.event_feed WHERE match_id = :m
        ORDER BY minute DESC, second DESC LIMIT 15
    """, m=int(match_id))
    st.dataframe(feed, use_container_width=True, hide_index=True)


live_page()

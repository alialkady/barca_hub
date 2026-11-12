#!/usr/bin/env python3
"""
StatsBomb open-data -> Postgres warehouse loader.

What it does, per match:
  1. downloads events + lineups from the StatsBomb open-data GitHub repo
  2. lands every event untouched in raw.statsbomb_events (JSONB)
  3. upserts dw.dim_season / dim_team / dim_match / dim_player
  4. extracts shots into dw.fact_shot
  5. records the run in raw.ingestion_log

Each match is its own transaction, and finished matches are skipped on re-run,
so you can stop and resume safely. Everything is idempotent (ON CONFLICT).

Examples
  python statsbomb_loader.py --list-seasons
  python statsbomb_loader.py --season-id 90 --limit-matches 3      # quick smoke test
  python statsbomb_loader.py --all-seasons                         # all Barca La Liga seasons
  python statsbomb_loader.py --competition-id 16 --all-seasons     # other competition
"""
import argparse
import os
import sys
import time

import psycopg2
import requests
from psycopg2.extras import Json, execute_values

BASE = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"
DEFAULT_DSN = "postgresql://barca:barca_dev_pw@localhost:5432/barca_dw"

http = requests.Session()


def fetch(path, retries=3):
    """GET a JSON file from the open-data repo. Retries network/5xx errors, not 4xx."""
    url = f"{BASE}/{path}"
    for attempt in range(1, retries + 1):
        try:
            r = http.get(url, timeout=60)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code < 500:
                raise  # a real 404 etc. - retrying won't help
            if attempt == retries:
                raise
        except requests.RequestException:
            if attempt == retries:
                raise
        time.sleep(2 * attempt)


# ----------------------------------------------------------------- upserts

def upsert_season(cur, competition_name, season_label):
    cur.execute(
        """
        INSERT INTO dw.dim_season (competition, season_label)
        VALUES (%s, %s)
        ON CONFLICT (competition, season_label)
            DO UPDATE SET competition = EXCLUDED.competition
        RETURNING season_key
        """,
        (competition_name, season_label),
    )
    return cur.fetchone()[0]


def upsert_team(cur, cache, sb_id, name, country):
    if sb_id in cache:
        return cache[sb_id]
    cur.execute(
        """
        INSERT INTO dw.dim_team (team_name, country, statsbomb_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (statsbomb_id) DO UPDATE
            SET team_name = EXCLUDED.team_name,
                country   = COALESCE(EXCLUDED.country, dw.dim_team.country)
        RETURNING team_key
        """,
        (name, country, sb_id),
    )
    cache[sb_id] = cur.fetchone()[0]
    return cache[sb_id]


def upsert_match(cur, season_key, m, home_key, away_key):
    # NOTE: StatsBomb 'kick_off' is local time with no timezone, so kickoff_utc
    # is left NULL on purpose rather than storing a wrong value.
    cur.execute(
        """
        INSERT INTO dw.dim_match
            (season_key, match_date, home_team_key, away_team_key,
             home_goals, away_goals, matchweek, stadium, referee, statsbomb_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (statsbomb_id) DO UPDATE
            SET home_goals = EXCLUDED.home_goals,
                away_goals = EXCLUDED.away_goals,
                matchweek  = EXCLUDED.matchweek,
                stadium    = EXCLUDED.stadium,
                referee    = EXCLUDED.referee
        RETURNING match_key
        """,
        (
            season_key,
            m["match_date"],
            home_key,
            away_key,
            m.get("home_score"),
            m.get("away_score"),
            m.get("match_week"),
            (m.get("stadium") or {}).get("name"),
            (m.get("referee") or {}).get("name"),
            m["match_id"],
        ),
    )
    return cur.fetchone()[0]


def upsert_players(cur, players):
    """players: {statsbomb_id: (full_name, known_as, nationality)} -> {statsbomb_id: player_key}"""
    if not players:
        return {}
    rows = [(pid, *vals) for pid, vals in players.items()]
    execute_values(
        cur,
        """
        INSERT INTO dw.dim_player (statsbomb_id, full_name, known_as, nationality)
        VALUES %s
        ON CONFLICT (statsbomb_id) DO UPDATE
            SET full_name   = EXCLUDED.full_name,
                known_as    = COALESCE(EXCLUDED.known_as,    dw.dim_player.known_as),
                nationality = COALESCE(EXCLUDED.nationality, dw.dim_player.nationality)
        """,
        rows,
    )
    cur.execute(
        "SELECT statsbomb_id, player_key FROM dw.dim_player WHERE statsbomb_id = ANY(%s)",
        (list(players),),
    )
    return dict(cur.fetchall())


# ----------------------------------------------------------------- per match

def players_from_lineups(lineups):
    out = {}
    for team in lineups:
        for p in team.get("lineup", []):
            out[p["player_id"]] = (
                p["player_name"],
                p.get("player_nickname"),
                (p.get("country") or {}).get("name"),
            )
    return out


def load_match(cur, season_key, m, team_cache, raw_mode):
    match_id = m["match_id"]
    home, away = m["home_team"], m["away_team"]

    home_key = upsert_team(cur, team_cache, home["home_team_id"], home["home_team_name"],
                           (home.get("country") or {}).get("name"))
    away_key = upsert_team(cur, team_cache, away["away_team_id"], away["away_team_name"],
                           (away.get("country") or {}).get("name"))
    match_key = upsert_match(cur, season_key, m, home_key, away_key)

    events = fetch(f"events/{match_id}.json")
    try:
        players = players_from_lineups(fetch(f"lineups/{match_id}.json"))
    except requests.HTTPError:
        players = {}

    # --- shots (period 5 = penalty shoot-out: not open-play data, so excluded)
    shots = [e for e in events if e["type"]["name"] == "Shot" and e.get("period") != 5]
    for e in shots:  # make sure every shooter exists in dim_player
        pl = e.get("player")
        if pl and pl["id"] not in players:
            players[pl["id"]] = (pl["name"], None, None)
    player_keys = upsert_players(cur, players)

    # --- raw layer
    if raw_mode != "none":
        raw_events = events if raw_mode == "all" else [e for e in events if e["type"]["name"] == "Shot"]
        execute_values(
            cur,
            "INSERT INTO raw.statsbomb_events (match_id, event_id, payload) VALUES %s "
            "ON CONFLICT DO NOTHING",
            [(match_id, e["id"], Json(e)) for e in raw_events],
            page_size=1000,
        )

    # --- fact_shot
    shot_rows = []
    for e in shots:
        s = e["shot"]
        loc = e.get("location") or [None, None]
        outcome = (s.get("outcome") or {}).get("name")
        shot_rows.append((
            match_key,
            player_keys[e["player"]["id"]],
            team_cache[e["team"]["id"]],
            e["minute"],
            e.get("second"),
            loc[0],
            loc[1],
            (s.get("body_part") or {}).get("name"),
            (s.get("type") or {}).get("name"),
            outcome,
            outcome == "Goal",
            s.get("statsbomb_xg"),
            e["id"],
        ))
    if shot_rows:
        execute_values(
            cur,
            """
            INSERT INTO dw.fact_shot
                (match_key, player_key, team_key, minute, second, x, y,
                 body_part, shot_type, outcome, is_goal, provider_xg, source_event_id)
            VALUES %s
            ON CONFLICT (source_event_id) DO NOTHING
            """,
            shot_rows,
        )

    cur.execute(
        "INSERT INTO raw.ingestion_log (source, dataset, rows_loaded, notes) VALUES (%s,%s,%s,%s)",
        ("statsbomb", f"events:{match_id}", len(events), f"shots={len(shot_rows)}"),
    )
    return len(events), len(shot_rows)


def already_loaded(cur, match_id):
    cur.execute(
        "SELECT 1 FROM raw.ingestion_log WHERE source = 'statsbomb' AND dataset = %s LIMIT 1",
        (f"events:{match_id}",),
    )
    return cur.fetchone() is not None


# ----------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--competition-id", type=int, default=11, help="11 = La Liga (default)")
    ap.add_argument("--season-id", type=int, nargs="*", default=[], help="one or more season ids")
    ap.add_argument("--all-seasons", action="store_true", help="every available season of the competition")
    ap.add_argument("--team", default="Barcelona", help="team name filter, or 'all' (default: Barcelona)")
    ap.add_argument("--limit-matches", type=int, help="only load the first N matches per season (testing)")
    ap.add_argument("--raw-events", choices=["all", "shots", "none"], default="all",
                    help="how much to keep in raw.statsbomb_events (default: all)")
    ap.add_argument("--force", action="store_true", help="re-download matches already loaded")
    ap.add_argument("--list-seasons", action="store_true", help="list seasons for the competition and exit")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN))
    args = ap.parse_args()

    competitions = [c for c in fetch("competitions.json") if c["competition_id"] == args.competition_id]
    if not competitions:
        sys.exit(f"No competition with id {args.competition_id} in the open data.")

    if args.list_seasons:
        for c in sorted(competitions, key=lambda c: c["season_name"], reverse=True):
            print(f"{c['competition_name']:<20} season_id={c['season_id']:<4} {c['season_name']}")
        return

    if args.all_seasons:
        wanted = competitions
    elif args.season_id:
        wanted = [c for c in competitions if c["season_id"] in args.season_id]
        missing = set(args.season_id) - {c["season_id"] for c in wanted}
        if missing:
            sys.exit(f"Unknown season id(s) for this competition: {sorted(missing)} (try --list-seasons)")
    else:
        sys.exit("Pick seasons with --season-id N [N ...] or --all-seasons (see --list-seasons).")

    conn = psycopg2.connect(args.dsn)
    team_cache = {}
    totals = {"matches": 0, "events": 0, "shots": 0, "skipped": 0}

    try:
        for comp in sorted(wanted, key=lambda c: c["season_name"]):
            label = comp["season_name"]
            matches = fetch(f"matches/{comp['competition_id']}/{comp['season_id']}.json")
            if args.team.lower() != "all":
                matches = [m for m in matches
                           if args.team in (m["home_team"]["home_team_name"], m["away_team"]["away_team_name"])]
            matches.sort(key=lambda m: (m["match_date"], m["match_id"]))
            if args.limit_matches:
                matches = matches[: args.limit_matches]

            print(f"\n== {comp['competition_name']} {label}: {len(matches)} match(es)")
            with conn, conn.cursor() as cur:
                season_key = upsert_season(cur, comp["competition_name"], label)

            for m in matches:
                tag = (f"{m['match_date']}  {m['home_team']['home_team_name']} "
                       f"{m.get('home_score')}-{m.get('away_score')} {m['away_team']['away_team_name']}")
                with conn, conn.cursor() as cur:  # one transaction per match
                    if not args.force and already_loaded(cur, m["match_id"]):
                        print(f"  skip   {tag}")
                        totals["skipped"] += 1
                        continue
                    n_events, n_shots = load_match(cur, season_key, m, team_cache, args.raw_events)
                print(f"  loaded {tag}  ({n_events} events, {n_shots} shots)")
                totals["matches"] += 1
                totals["events"] += n_events
                totals["shots"] += n_shots
    finally:
        conn.close()

    print(f"\nDone: {totals['matches']} loaded, {totals['skipped']} skipped, "
          f"{totals['events']} events, {totals['shots']} shots.")


if __name__ == "__main__":
    main()

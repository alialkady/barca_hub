#!/usr/bin/env python3
"""
Replay a past match into Redpanda as if it were live.

Reads the match's events from raw.statsbomb_events (already loaded by the loader) and
publishes them to the topic at the original pace, divided by --speed.
When you later get a real live feed, only this file changes; the consumer stays the same.

  python replay_producer.py --list
  python replay_producer.py --match-id 3773497 --speed 60      # 90 min match in ~1.6 min
  python replay_producer.py --match-id 3773497 --speed 0       # as fast as possible
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import psycopg2

from kafka_util import ensure_topic
from live_metrics import clamp_monotonic, period_offsets, trim_event

DEFAULT_DSN = "postgresql://barca:barca_dev_pw@localhost:5432/barca_dw"
DEFAULT_BOOTSTRAP = "localhost:19092"      # inside Docker the compose file sets redpanda:9092
TOPIC = "match.events"


def list_matches(conn, team, limit):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.statsbomb_id, m.match_date, th.team_name, m.home_goals, m.away_goals, ta.team_name
            FROM dw.dim_match m
            JOIN dw.dim_team th ON th.team_key = m.home_team_key
            JOIN dw.dim_team ta ON ta.team_key = m.away_team_key
            WHERE m.statsbomb_id IS NOT NULL AND (%s = 'all' OR %s IN (th.team_name, ta.team_name))
            ORDER BY m.match_date DESC LIMIT %s
            """,
            (team, team, limit),
        )
        for mid, d, h, hg, ag, a in cur.fetchall():
            print(f"{mid}  {d}  {h} {hg}-{ag} {a}")


def build_messages(conn, match_id):
    """-> (meta_start_message, [event messages in match order])"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT th.statsbomb_id, th.team_name, ta.statsbomb_id, ta.team_name
            FROM dw.dim_match m
            JOIN dw.dim_team th ON th.team_key = m.home_team_key
            JOIN dw.dim_team ta ON ta.team_key = m.away_team_key
            WHERE m.statsbomb_id = %s
            """,
            (match_id,),
        )
        row = cur.fetchone()
        if row is None:
            sys.exit(f"Match {match_id} is not in dw.dim_match. Run --list to see valid ids.")
        cur.execute(
            """
            SELECT payload FROM raw.statsbomb_events WHERE match_id = %s
            ORDER BY (payload->>'period')::int, (payload->>'index')::int
            """,
            (match_id,),
        )
        events = [r[0] for r in cur.fetchall()]
    if not events:
        sys.exit(f"No raw events for match {match_id}.")

    meta = {
        "kind": "meta", "action": "start", "match_id": match_id,
        "home": {"id": row[0], "name": row[1]}, "away": {"id": row[2], "name": row[3]},
    }
    offsets = period_offsets(events)
    return meta, clamp_monotonic([trim_event(e, match_id, offsets) for e in events])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list matches you can replay and exit")
    ap.add_argument("--team", default="Barcelona", help="team filter for --list, or 'all'")
    ap.add_argument("--limit", type=int, default=40, help="rows for --list")
    ap.add_argument("--match-id", type=int, help="StatsBomb match id to replay")
    ap.add_argument("--speed", type=float, default=60.0, help="60 = one minute of play per second; 0 = no delay")
    ap.add_argument("--topic", default=TOPIC)
    ap.add_argument("--bootstrap", default=os.environ.get("KAFKA_BOOTSTRAP", DEFAULT_BOOTSTRAP))
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN))
    args = ap.parse_args()

    conn = psycopg2.connect(args.dsn)
    if args.list:
        list_matches(conn, args.team, args.limit)
        return
    if args.match_id is None:
        sys.exit("Give --match-id (see --list).")

    meta, messages = build_messages(conn, args.match_id)
    conn.close()

    from confluent_kafka import Producer          # imported late so --list works without a broker

    ensure_topic(args.bootstrap, args.topic)
    producer = Producer({"bootstrap.servers": args.bootstrap})
    failures = []

    def on_delivery(err, _msg):
        if err is not None:
            failures.append(err)

    key = str(args.match_id)

    def send(payload):
        producer.produce(args.topic, key=key, value=json.dumps(payload), on_delivery=on_delivery)
        producer.poll(0)

    meta["started_at"] = datetime.now(timezone.utc).isoformat()
    send(meta)
    print(f"Replaying match {args.match_id}: {meta['home']['name']} vs {meta['away']['name']} "
          f"({len(messages)} events, speed x{args.speed:g})")

    t0, c0, last_minute = time.monotonic(), messages[0]["clock_s"], -1
    try:
        for m in messages:
            if args.speed > 0:
                delay = t0 + (m["clock_s"] - c0) / args.speed - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            send(m)
            minute = int(m["clock_s"] // 60)
            if minute != last_minute and minute % 5 == 0:
                print(f"  match minute {minute}")
            last_minute = minute
    except KeyboardInterrupt:
        print("Interrupted; flushing what was sent so far.")

    send({"kind": "meta", "action": "end", "match_id": args.match_id})
    producer.flush(30)
    if failures:
        sys.exit(f"{len(failures)} message(s) failed to deliver, first error: {failures[0]}")
    print("Done.")


if __name__ == "__main__":
    main()

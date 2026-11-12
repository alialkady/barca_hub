#!/usr/bin/env python3
"""
Reads match events from Redpanda, keeps a live scoreboard per match (live_metrics.MatchState)
and writes it to Postgres: live.match_state (one row per minute) and live.event_feed.

It reads the topic from the beginning every time it starts and does not commit offsets.
Each replay begins with a 'start' message that wipes that match's live rows, so the tables
always end up reflecting the latest replay.
"""
import argparse
import json
import os

import psycopg2
from psycopg2.extras import Json, execute_values

from kafka_util import ensure_topic
from live_metrics import MatchState

DEFAULT_DSN = "postgresql://barca:barca_dev_pw@localhost:5432/barca_dw"
DEFAULT_BOOTSTRAP = "localhost:19092"
TOPIC = "match.events"


class PgSink:
    def __init__(self, dsn):
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True

    def reset(self, match_id):
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM live.match_state WHERE match_id = %s", (match_id,))
            cur.execute("DELETE FROM live.event_feed WHERE match_id = %s", (match_id,))

    def write_snapshot(self, match_id, s):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO live.match_state
                    (match_id, minute, home_goals, away_goals, home_xg, away_xg, home_possession,
                     home_shots, away_shots, home_pressures, away_pressures, momentum, updated_at)
                VALUES (%(match_id)s, %(minute)s, %(home_goals)s, %(away_goals)s, %(home_xg)s, %(away_xg)s,
                        %(home_possession)s, %(home_shots)s, %(away_shots)s, %(home_pressures)s,
                        %(away_pressures)s, %(momentum)s, now())
                ON CONFLICT (match_id, minute) DO UPDATE SET
                    home_goals = EXCLUDED.home_goals, away_goals = EXCLUDED.away_goals,
                    home_xg = EXCLUDED.home_xg, away_xg = EXCLUDED.away_xg,
                    home_possession = EXCLUDED.home_possession,
                    home_shots = EXCLUDED.home_shots, away_shots = EXCLUDED.away_shots,
                    home_pressures = EXCLUDED.home_pressures, away_pressures = EXCLUDED.away_pressures,
                    momentum = EXCLUDED.momentum, updated_at = now()
                """,
                {**s, "match_id": match_id},
            )

    def write_feed(self, match_id, items):
        with self.conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO live.event_feed
                    (match_id, event_id, minute, second, event_type, team, player, detail)
                VALUES %s ON CONFLICT DO NOTHING
                """,
                [(match_id, i["event_id"], i["minute"], i["second"], i["event_type"],
                  i["team"], i["player"], Json(i["detail"])) for i in items],
            )


class LiveProcessor:
    """Turns messages into state + database writes. Knows nothing about Kafka (easy to test)."""

    def __init__(self, sink, flush_every=20):
        self.sink, self.flush_every = sink, flush_every
        self.states, self.counts = {}, {}

    def handle(self, msg):
        mid = msg["match_id"]
        if msg["kind"] == "meta":
            if msg["action"] == "start":
                self.sink.reset(mid)
                h, a = msg["home"], msg["away"]
                self.states[mid] = MatchState(mid, h["id"], h["name"], a["id"], a["name"])
                self.counts[mid] = 0
                self.sink.write_snapshot(mid, self.states[mid].snapshot())
            elif msg["action"] == "end" and mid in self.states:
                self.sink.write_snapshot(mid, self.states[mid].snapshot())
            return

        state = self.states.get(mid)
        if state is None:                      # event without a 'start' message: ignore
            return
        before = state.snapshot()
        event_minute = int(msg["clock_s"] // 60)
        if msg["period"] != 5 and event_minute > before["minute"]:
            self.sink.write_snapshot(mid, before)      # final picture of the minute that just ended
            for k in range(before["minute"] + 1, event_minute):   # minutes with no events at all
                state.advance_to(k * 60.0)
                self.sink.write_snapshot(mid, state.snapshot())
        feed = state.apply(msg)
        after = state.snapshot()
        if feed:
            self.sink.write_feed(mid, feed)
        self.counts[mid] += 1
        if self.counts[mid] % self.flush_every == 0 or any(f["event_type"] == "Goal" for f in feed):
            self.sink.write_snapshot(mid, after)       # keep the dashboard fresh inside a minute


def run(bootstrap, topic, dsn, group):
    from confluent_kafka import Consumer, KafkaError   # imported late so tests don't need the library

    ensure_topic(bootstrap, topic)
    consumer = Consumer({
        "bootstrap.servers": bootstrap,
        "group.id": group,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe([topic])
    processor = LiveProcessor(PgSink(dsn))
    print(f"Consuming '{topic}' from {bootstrap} ...", flush=True)
    try:
        while True:
            m = consumer.poll(1.0)
            if m is None:
                continue
            if m.error():
                if m.error().code() != KafkaError._PARTITION_EOF:
                    print("Kafka error:", m.error(), flush=True)
                continue
            processor.handle(json.loads(m.value()))
    except KeyboardInterrupt:
        pass
    finally:
        consumer.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--topic", default=TOPIC)
    ap.add_argument("--group", default="live-metrics")
    ap.add_argument("--bootstrap", default=os.environ.get("KAFKA_BOOTSTRAP", DEFAULT_BOOTSTRAP))
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN))
    a = ap.parse_args()
    run(a.bootstrap, a.topic, a.dsn, a.group)

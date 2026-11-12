"""Run from the streaming/ folder:  python -m pytest -q   (pip install -r requirements-dev.txt)"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from live_consumer import LiveProcessor  # noqa: E402
from live_metrics import MatchState, clamp_monotonic, period_offsets  # noqa: E402

HOME, AWAY = 1, 2


def ev(type_, team, clock_s=60.0, period=1, **kw):
    base = {"kind": "event", "match_id": 9, "event_id": f"e-{type_}-{clock_s}-{team}", "index": 1,
            "period": period, "clock_s": clock_s, "type": type_, "team_id": team,
            "team": "H" if team == HOME else "A", "player_id": 7, "player": "P",
            "x": 50.0, "y": 40.0, "shot_xg": None, "shot_outcome": None,
            "pass_outcome": None, "card": None, "sub_on": None}
    base.update(kw)
    return base


def new_state():
    return MatchState(9, HOME, "H", AWAY, "A")


def test_goal_counts_and_feed():
    s = new_state()
    feed = s.apply(ev("Shot", HOME, shot_xg=0.3, shot_outcome="Goal"))
    assert s.goals == {"home": 1, "away": 0}
    assert feed[0]["event_type"] == "Goal"
    snap = s.snapshot()
    assert snap["home_shots"] == 1 and snap["home_xg"] == 0.3


def test_saved_shot_is_not_a_goal():
    s = new_state()
    s.apply(ev("Shot", AWAY, shot_xg=0.1, shot_outcome="Saved"))
    assert s.goals == {"home": 0, "away": 0} and s.shots["away"] == 1


def test_own_goal_credits_the_beneficiary():
    s = new_state()
    s.apply(ev("Own Goal For", HOME))         # StatsBomb records it under the team that benefits
    s.apply(ev("Own Goal Against", AWAY))     # ...and this one under the player who scored it
    assert s.goals == {"home": 1, "away": 0}


def test_shootout_is_ignored():
    s = new_state()
    s.apply(ev("Shot", HOME, period=5, shot_xg=0.78, shot_outcome="Goal"))
    assert s.goals == {"home": 0, "away": 0} and s.shots["home"] == 0


def test_possession_is_pass_share():
    s = new_state()
    for i in range(3):
        s.apply(ev("Pass", HOME, clock_s=float(i)))
    s.apply(ev("Pass", AWAY, clock_s=4.0))
    assert s.snapshot()["home_possession"] == 75.0


def test_momentum_sign_and_decay():
    s = new_state()
    s.apply(ev("Shot", HOME, clock_s=100.0, shot_xg=0.5, shot_outcome="Saved"))
    assert s.snapshot()["momentum"] > 0
    s.apply(ev("Pressure", AWAY, clock_s=100.0 + 601))   # 10+ minutes later, nothing else happened
    assert s.snapshot()["momentum"] == 0


def test_half_markers_announced_once():
    s = new_state()
    feed = s.apply(ev("Half Start", HOME)) + s.apply(ev("Half Start", AWAY))
    assert len(feed) == 1


def test_period_offsets_keep_clock_increasing():
    events = [{"period": 1, "timestamp": "00:47:10.000"}, {"period": 2, "timestamp": "00:00:00.000"},
              {"period": 2, "timestamp": "00:46:00.000"}]
    off = period_offsets(events)
    assert off[1] == 0 and off[2] == 47 * 60 + 10


def test_clamp_monotonic_fixes_bad_timestamp():
    msgs = [{"clock_s": 2750.0}, {"clock_s": 1.1}, {"clock_s": 2756.9}]   # bad receipt before half time
    assert [m["clock_s"] for m in clamp_monotonic(msgs)] == [2750.0, 2750.0, 2756.9]


class RecordingSink:
    def __init__(self):
        self.snapshots, self.feed = [], []

    def reset(self, match_id):
        pass

    def write_snapshot(self, match_id, snap):
        self.snapshots.append(snap)

    def write_feed(self, match_id, items):
        self.feed.extend(items)


def test_every_minute_gets_a_row_even_when_nothing_happens():
    sink = RecordingSink()
    proc = LiveProcessor(sink, flush_every=1000)
    proc.handle({"kind": "meta", "action": "start", "match_id": 9,
                 "home": {"id": HOME, "name": "H"}, "away": {"id": AWAY, "name": "A"}})
    proc.handle(ev("Shot", HOME, clock_s=10.0, shot_xg=0.2, shot_outcome="Saved"))
    proc.handle(ev("Pass", AWAY, clock_s=5 * 60 + 2.0))          # nothing at all in minutes 1-4
    proc.handle({"kind": "meta", "action": "end", "match_id": 9})
    minutes = sorted({s["minute"] for s in sink.snapshots})
    assert minutes == [0, 1, 2, 3, 4, 5]
    by_minute = {s["minute"]: s for s in sink.snapshots}
    assert by_minute[0]["home_shots"] == 1 and by_minute[3]["home_shots"] == 1   # state carried over

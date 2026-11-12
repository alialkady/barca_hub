"""
Pure logic for live match metrics: no Kafka and no database in here, so it is easy to test.

Two jobs:
  * trim_event()  - shrink a raw StatsBomb event into the small message we publish
  * MatchState    - fold those messages, one by one, into a live scoreboard

CLOCK: StatsBomb timestamps restart at 0 in every period, and stoppage time makes the
first half longer than 45 minutes. period_offsets() stitches the periods into one
continuous clock (seconds of play, halftime excluded) so time never jumps backwards.
"minute" in the live tables is int(clock / 60) on that clock, so it can differ by a
minute or two from the broadcast clock in the second half.

DEFINITIONS (all simple on purpose; change them freely)
  score        goals from Shot events (outcome Goal) + 'Own Goal For' events.
               Penalty shoot-out events (period 5) are ignored.
  xg / shots   StatsBomb xG and shot count, shoot-outs excluded.
  possession   share of all passes made so far (pass share, NOT time on the ball).
  pressures    count of Pressure events.
  momentum     over the last 10 minutes of play, each team earns "threat":
                 shot -> 10 x its xG,   completed pass or carry starting in the
                 attacking third (x >= 80) -> 0.1.
               momentum = 100 * (home - away) / (home + away + 1), so it lies between
               -100 and +100; positive means the home team is on top. The +1 keeps a
               quiet spell from looking like total dominance.
"""
from collections import deque

WINDOW_S = 600
FINAL_THIRD_X = 80
SHOT_WEIGHT = 10.0
ACTION_WEIGHT = 0.1


# ------------------------------------------------------------------ clock

def _ts_seconds(ts):
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def period_offsets(events):
    """{period: seconds to add to that period's timestamps} so the match clock never goes back."""
    length = {}
    for e in events:
        t = _ts_seconds(e["timestamp"])
        length[e["period"]] = max(length.get(e["period"], 0.0), t)
    offsets, acc = {}, 0.0
    for period in sorted(length):
        offsets[period] = acc
        acc += length[period]
    return offsets


# ------------------------------------------------------------------ message shape

def trim_event(e, match_id, offsets):
    """Raw StatsBomb event -> the small dict we publish to Kafka."""
    loc = e.get("location") or [None, None]
    shot = e.get("shot") or {}
    return {
        "kind": "event",
        "match_id": match_id,
        "event_id": e["id"],
        "index": e["index"],
        "period": e["period"],
        "clock_s": round(offsets[e["period"]] + _ts_seconds(e["timestamp"]), 3),
        "type": e["type"]["name"],
        "team_id": (e.get("team") or {}).get("id"),
        "team": (e.get("team") or {}).get("name"),
        "player_id": (e.get("player") or {}).get("id"),
        "player": (e.get("player") or {}).get("name"),
        "x": loc[0],
        "y": loc[1],
        "shot_xg": shot.get("statsbomb_xg"),
        "shot_outcome": (shot.get("outcome") or {}).get("name"),
        "pass_outcome": ((e.get("pass") or {}).get("outcome") or {}).get("name"),
        "card": ((e.get("foul_committed") or {}).get("card")
                 or (e.get("bad_behaviour") or {}).get("card") or {}).get("name"),
        "sub_on": (((e.get("substitution") or {}).get("replacement")) or {}).get("name"),
    }


def clamp_monotonic(messages):
    """Never let the clock go backwards. StatsBomb has a few events with a bad timestamp
    (e.g. a 'Ball Receipt*' logged just before half time with minute 0, second 1); such an
    event simply inherits the clock of the event before it. Mutates and returns the list."""
    last = 0.0
    for m in messages:
        if m["clock_s"] < last:
            m["clock_s"] = last
        last = m["clock_s"]
    return messages


# ------------------------------------------------------------------ live state

class MatchState:
    def __init__(self, match_id, home_id, home_name, away_id, away_name):
        self.match_id = match_id
        self.side_of = {home_id: "home", away_id: "away"}
        self.names = {"home": home_name, "away": away_name}
        self.clock_s = 0.0
        self.goals = {"home": 0, "away": 0}
        self.shots = {"home": 0, "away": 0}
        self.passes = {"home": 0, "away": 0}
        self.pressures = {"home": 0, "away": 0}
        self.xg = {"home": 0.0, "away": 0.0}
        self._window = deque()                       # (clock_s, side, weight)
        self._wsum = {"home": 0.0, "away": 0.0}      # running totals of the window
        self._marks = set()                          # half start/end already announced

    # -- helpers
    def _push(self, ev, side, weight):
        if weight > 0:
            self._window.append((ev["clock_s"], side, weight))
            self._wsum[side] += weight

    def _prune(self):
        cutoff = self.clock_s - WINDOW_S
        while self._window and self._window[0][0] < cutoff:
            _, side, weight = self._window.popleft()
            self._wsum[side] -= weight

    def _feed(self, ev, event_type, detail=None):
        return {
            "event_id": ev["event_id"],
            "minute": int(ev["clock_s"] // 60),
            "second": int(ev["clock_s"] % 60),
            "event_type": event_type,
            "team": ev.get("team"),
            "player": ev.get("player"),
            "detail": detail or {},
        }

    # -- main entry point: returns the list of "notable" events for the live feed
    def apply(self, ev):
        if ev["period"] == 5:        # penalty shoot-out is not part of the match flow
            return []
        self.clock_s = max(self.clock_s, ev["clock_s"])
        side = self.side_of.get(ev["team_id"])
        if side is None:
            return []
        t, feed = ev["type"], []

        if t == "Shot":
            xg = ev.get("shot_xg") or 0.0
            self.shots[side] += 1
            self.xg[side] += xg
            self._push(ev, side, SHOT_WEIGHT * xg)
            goal = ev.get("shot_outcome") == "Goal"
            if goal:
                self.goals[side] += 1
            feed.append(self._feed(ev, "Goal" if goal else "Shot",
                                   {"outcome": ev.get("shot_outcome"), "xg": round(xg, 3)}))
        elif t == "Own Goal For":                    # recorded under the team that benefits
            self.goals[side] += 1
        elif t == "Own Goal Against":                # recorded under the player who scored it
            feed.append(self._feed(ev, "Own Goal"))
        elif t == "Pass":
            if ev.get("pass_outcome") != "Injury Clearance":
                self.passes[side] += 1
            if ev.get("pass_outcome") is None and (ev.get("x") or 0) >= FINAL_THIRD_X:
                self._push(ev, side, ACTION_WEIGHT)
        elif t == "Carry":
            if (ev.get("x") or 0) >= FINAL_THIRD_X:
                self._push(ev, side, ACTION_WEIGHT)
        elif t == "Pressure":
            self.pressures[side] += 1
        elif t in ("Foul Committed", "Bad Behaviour") and ev.get("card"):
            feed.append(self._feed(ev, "Card", {"card": ev["card"]}))
        elif t == "Substitution":
            feed.append(self._feed(ev, "Substitution", {"on": ev.get("sub_on")}))
        elif t in ("Half Start", "Half End"):
            if (t, ev["period"]) not in self._marks:   # both teams emit it; announce once
                self._marks.add((t, ev["period"]))
                item = self._feed(ev, t, {"period": ev["period"]})
                item["team"] = item["player"] = None       # a match event, not a team's
                feed.append(item)
        return feed

    def advance_to(self, clock_s):
        """Move the clock forward with no event (used to fill minutes in which nothing happened)."""
        self.clock_s = max(self.clock_s, clock_s)

    def snapshot(self):
        """Row for live.match_state at the current clock."""
        self._prune()
        h, a = self._wsum["home"], self._wsum["away"]
        total_passes = self.passes["home"] + self.passes["away"]
        return {
            "minute": int(self.clock_s // 60),
            "home_goals": self.goals["home"],
            "away_goals": self.goals["away"],
            "home_xg": round(self.xg["home"], 3),
            "away_xg": round(self.xg["away"], 3),
            "home_possession": round(100.0 * self.passes["home"] / total_passes, 1) if total_passes else None,
            "home_shots": self.shots["home"],
            "away_shots": self.shots["away"],
            "home_pressures": self.pressures["home"],
            "away_pressures": self.pressures["away"],
            "momentum": round(100.0 * (h - a) / (h + a + 1.0), 2),
        }

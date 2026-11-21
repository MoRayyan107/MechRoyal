"""Offline tests for the bot (standard library only). There is no game simulator in the kit,
so this checks the things we *can* check without one:

  1. replay:  every recorded observation in check/stream.jsonl.gz, timing each answer
  2. fuzz:    thousands of synthetic observations (random spots on the map, every zone phase,
              crowds of enemies and bullets, missing / null / odd fields), checking the bot
              never raises out of act(), always echoes the tick, and answers JSON-serialisable
  3. routes:  a crude walker (walk speed, walls stop you, no jumping) follows the bot's move
              commands from random start points toward random zone circles, and reports how
              often it gets inside in time. This tests the route planner, not the game.

Usage:  python tools/stress.py [--seed N] [--fuzz N] [--routes N]
Exit code 1 if anything fails (exception escaping act, bad tick, slow answer, bad JSON).
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import math
import os
import random
import statistics
import sys
import time
import contextlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot as botmod  # noqa: E402
import nav  # noqa: E402

LIMIT_MS = 15.0   # our own target per answer (the server allows 50)


def load_stream():
    with gzip.open(os.path.join(ROOT, "check", "stream.jsonl.gz"), "rt", encoding="utf-8") as fh:
        msgs = [json.loads(line) for line in fh if line.strip()]
    hello = next(m for m in msgs if m.get("type") == "hello")
    hello = dict(hello)
    hello["map"] = dict(hello["map"], dir=os.path.join(ROOT, "arena"))
    obs = [m for m in msgs if m.get("type") == "obs"]
    return hello, obs


def timed(b, o, times, failures):
    stderr = io.StringIO()
    t = time.perf_counter()
    with contextlib.redirect_stderr(stderr):
        try:
            a = b.act(o)
        except Exception as e:  # act() must never raise
            failures.append(f"tick {o.get('tick')}: act raised {e!r}")
            return None
    dt = (time.perf_counter() - t) * 1000
    times.append(dt)
    if "decide failed" in stderr.getvalue():
        failures.append(f"tick {o.get('tick')}: decide fell back\n{stderr.getvalue()[-600:]}")
    if a.get("tick") != o.get("tick"):
        failures.append(f"tick {o.get('tick')}: wrong tick {a.get('tick')}")
    try:
        line = json.dumps(a)
        if len(line) > 65536:
            failures.append(f"tick {o.get('tick')}: line too long")
    except (TypeError, ValueError) as e:
        failures.append(f"tick {o.get('tick')}: not JSON {e!r}")
    return a


def rand_zone(rng, t):
    """A plausible zone message for match time t (random centres, real phase radii)."""
    phases = [(60, 50, 380), (45, 40, 240), (35, 35, 140), (30, 30, 70), (25, 25, 25), (15, 25, 0)]
    c = [41.3, -72.8]
    r = 560.0
    start = 0.0
    for hold, shrink, nr in phases:
        ang = rng.uniform(0, 2 * math.pi)
        off = rng.uniform(0, r - nr)
        nc = [c[0] + off * math.cos(ang), c[1] + off * math.sin(ang)]
        ss, se = start + hold, start + hold + shrink
        if t < se or nr == 0:
            return {"center": c, "radius": r, "next_center": nc, "next_radius": nr,
                    "shrink_start": ss, "shrink_end": se, "dps": 20}
        c, r, start = nc, float(nr), se
    return None


def fake_obs(rng, grid, tick, base):
    t = tick / 20.0
    x = rng.uniform(grid.minx + 10, grid.minx + grid.nx * nav.CELL - 10)
    y = rng.uniform(grid.miny + 10, grid.miny + grid.ny * nav.CELL - 10)
    z = grid.terrain_h(x, y) + rng.choice([0, 0, 0, 25])
    o = json.loads(json.dumps(base))
    o["tick"] = tick
    o["t"] = t
    me = o["self"]
    me["pos"] = [x, y, z]
    me["vel"] = [rng.uniform(-12, 12), rng.uniform(-12, 12), 0]
    me["aim"] = [rng.uniform(-3.1, 3.1), rng.uniform(-0.5, 0.5)]
    me["in_zone"] = rng.random() < 0.7
    me["energy"] = rng.uniform(0, 100)
    me["grounded"] = rng.random() < 0.8
    o["zone"] = rand_zone(rng, t)
    mechs = []
    for k in range(rng.choice([0, 0, 1, 3, 15])):
        mechs.append({"id": k if k != me["id"] else 15, "kind": rng.choice(["mech", "mech", "npc"]),
                      "pos": [x + rng.uniform(-150, 150), y + rng.uniform(-150, 150), z],
                      "vel": [rng.uniform(-15, 15), rng.uniform(-15, 15), 0],
                      "aim": [0, 0], "hp": 500, "max_hp": 1400, "weapons": ["gun"]})
    o["mechs"] = mechs
    o["projectiles"] = [{"id": 300 + k, "kind": "gun", "pos": [x + k, y, z], "vel": [100, 0, 0]}
                        for k in range(rng.choice([0, 5, 200]))]
    ev = []
    if rng.random() < 0.3:
        ev.append({"type": "damaged", "amount": 45, "weapon": "gun", "bearing": rng.uniform(-3, 3),
                   "by": rng.choice([None, 3, 104, 12])})
    if rng.random() < 0.05:
        ev.append({"type": "damaged", "amount": 20, "weapon": "zone", "bearing": None, "by": None})
    o["events"] = ev
    if rng.random() < 0.1:
        o["offer"] = {"id": 1, "expires": t + 5, "choices": [
            {"id": rng.choice(["gun_rate", "plating", "sensors", "zzz"]), "name": "a", "desc": "b"},
            {"id": "armor", "name": "a", "desc": "b"}, {"id": "jets", "name": "a", "desc": "b"}]}
    else:
        o["offer"] = None
    # odd but schema-plausible shapes
    if rng.random() < 0.05:
        me["weapons"] = [None, None]
    if rng.random() < 0.05:
        o.pop("events", None)
    return o


def route_test(hello, rng, n, failures):
    """Crude walker: 10 decisions/s at walk speed, cells marked blocked stop it (with sliding)."""
    b = botmod.Bot(hello)
    g = b.grid
    reached = 0
    stuck_runs = 0
    total_t = []
    for trial in range(n):
        # random walkable start, random goal circle of a random size
        while True:
            x = rng.uniform(g.minx + 20, g.minx + g.nx * nav.CELL - 20)
            y = rng.uniform(g.miny + 20, g.miny + g.ny * nav.CELL - 20)
            if not g.blocked[g.cell_of(x, y)]:
                break
        nr = rng.choice([240, 140, 70, 25, 0])
        while True:
            gx = rng.uniform(-300, 300)
            gy = rng.uniform(-250, 250)
            if math.hypot(gx - x, gy - y) > nr + 50:
                break
        b2 = botmod.Bot.__new__(botmod.Bot)
        b2.__dict__.update(b.__dict__)
        b2.field_key = None
        b2.hist = []
        b2.unstick_until = -1.0
        zone = {"center": [gx, gy], "radius": 2000, "next_center": [gx, gy], "next_radius": nr,
                "shrink_start": 1e9, "shrink_end": 1e9 + 1, "dps": 20}
        goal_r = max(nr - min(20.0, 0.3 * nr), 5.0)
        ok = False
        t = 0.0
        start_d = math.hypot(gx - x, gy - y)
        budget = 30 + 3.0 * start_d / 4.5   # the bot walks quietly (4.5 m/s) when the zone allows
        while t < budget:
            o = {"type": "obs", "tick": int(t * 20), "t": t, "alive": 16,
                 "self": {"id": 0, "pos": [x, y, g.surf[g.cell_of(x, y)]], "vel": [0, 0, 0], "aim": [0, 0],
                          "hp": 1400, "max_hp": 1400, "energy": 50, "max_energy": 100, "grounded": True,
                          "cubes": 0, "upgrades": [], "in_zone": True, "weapons": [None, None],
                          "stats": {"walk_speed": 11.7}},
                 "mechs": [], "projectiles": [], "crates": [], "cubes": [], "pickups": [], "sounds": [],
                 "beams": [], "events": [], "drops": [], "zone": zone, "offer": None}
            with contextlib.redirect_stderr(io.StringIO()):
                a = b2.act(o)
            mv = a.get("move") or [0, 0]
            sp = 11.7 * 0.1
            nx_, ny_ = x + mv[0] * sp, y + mv[1] * sp
            if not g.blocked[g.cell_of(nx_, ny_)]:
                x, y = nx_, ny_
            elif not g.blocked[g.cell_of(nx_, y)]:
                x = nx_
            elif not g.blocked[g.cell_of(x, ny_)]:
                y = ny_
            t += 0.1
            f = b2.field
            # success = standing on the chosen spot (field value 0), which must lie inside the goal circle
            arrived = f is not None and f.done and f.value(g.cell_of(x, y)) == 0.0
            if nr <= 25 and math.hypot(gx - x, gy - y) <= 8.0:
                arrived = True      # endgame: the bot holds within 8 m of the centre
            if arrived:
                if b2.spot is not None and math.hypot(b2.spot[0] - gx, b2.spot[1] - gy) > goal_r + 3.0:
                    failures.append(f"route {trial}: spot {b2.spot} outside goal circle r={goal_r:.0f}")
                centre_blocked = g.blocked[g.cell_of(gx, gy)]
                # (the walker cannot jet onto a roof, so a centre inside a building is exempt)
                if math.hypot(gx - x, gy - y) > goal_r + 6.0 and not (nr <= 25 and centre_blocked):
                    failures.append(f"route {trial}: stopped {math.hypot(gx - x, gy - y):.0f} m from centre, goal r={goal_r:.0f} (next r={nr})")
                ok = True
                break
        if ok:
            reached += 1
            total_t.append(t / max(start_d / 11.7, 1.0))
        else:
            stuck_runs += 1
            if stuck_runs <= 5:
                print(f"  route {trial}: from ({x:.0f},{y:.0f}) never reached ({gx:.0f},{gy:.0f}) r={goal_r:.0f}"
                      f" (started {start_d:.0f} m away; field here {b2.field.value(g.cell_of(x, y)):.0f})")
    print(f"routes: reached {reached}/{n}; time vs straight-line walk: median "
          f"{statistics.median(total_t) if total_t else 0:.2f}x, worst {max(total_t) if total_t else 0:.2f}x")
    if reached < 0.95 * n:
        failures.append(f"routes: only {reached}/{n} reached the zone")


def report(name, times):
    times = sorted(times)
    if not times:
        return
    p = lambda q: times[min(len(times) - 1, int(q * len(times)))]
    print(f"{name}: {len(times)} answers, ms p50 {p(.5):.2f} p99 {p(.99):.2f} max {times[-1]:.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--fuzz", type=int, default=3000)
    ap.add_argument("--routes", type=int, default=60)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    failures = []
    hello, stream = load_stream()

    t = time.perf_counter()
    with contextlib.redirect_stderr(io.StringIO()):
        b = botmod.Bot(hello)
    print(f"start-up: {time.perf_counter() - t:.2f}s")

    times = []
    for o in stream:
        timed(b, o, times, failures)
    report("replay", times)

    times = []
    base = stream[-1]
    for k in range(args.fuzz):
        timed(b, fake_obs(rng, b.grid, 2000 + k * 2, base), times, failures)
    report("fuzz", times)
    if times and max(times) > LIMIT_MS:
        failures.append(f"fuzz: slowest answer {max(times):.1f} ms > {LIMIT_MS} ms")

    # a broken map directory must not crash start-up or ticks
    bad = dict(hello, map={"dir": "/nonexistent", "files": []})
    with contextlib.redirect_stderr(io.StringIO()):
        b3 = botmod.Bot(bad)
    times = []
    for o in stream[:50]:
        timed(b3, o, times, failures)
    report("no-map", times)

    if args.routes:
        route_test(hello, rng, args.routes, failures)

    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures[:20]:
            print("  " + f)
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()

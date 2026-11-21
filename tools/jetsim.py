"""Crude jump-and-jet simulator for testing the sub-06 rooftop rule offline (standard library only).

The kit has no game simulator. This is OUR model of the movement rules in protocol.md section 7, with the numbers
from hello.config.mech, run against the exact building prisms in arena/v2/world.json. It is a test tool, not the
game: where protocol.md is silent we had to guess, and those guesses are marked GUESS below.

  From protocol.md / config (not guesses): 20 server ticks/s, one decision per 2 ticks; walk 11.7 m/s, boost
  19.5 m/s on the ground (17 energy/s); ground accel 58.5, air accel 12 m/s^2; gravity 22; jump 11 m/s if grounded;
  jets in the air: +34 m/s^2 up, capped at 10 m/s rise, 15 energy/s; energy regen 18/s after 0.6 s without
  spending; ledges up to 1.2 m are walked up; walls stop the part of a move that goes into them (slide); a solid
  above the head stops a jet; coming down past the edge of a roof you overhang you land on the edge.
  GUESS: horizontal target speed in the air is walk speed (protocol only says boost in the air = jets); wall
  contact is tested at 12 points on the 2.5 m circle plus the centre; landing on a roof edge then walking onto it
  works whenever the feet are at the roof height; no slide-off from an edge; no mech-vs-mech pushing; no slopes
  limit (terrain is followed); pickup centre 0.4 m and top 0.8 m above its pos (for line of sight).

Usage:
  python tools/jetsim.py validate          physics sanity checks (straight-up climb on 100 energy, ...)
  python tools/jetsim.py recorded          the four rooftop pickups of the recorded match, from its spawn
  python tools/jetsim.py sweep [--n N]     random rooftop pickups across the map, random starts 15-80 m away
  python tools/jetsim.py freq [--n N]      how often a spawn-like position has a qualifying rooftop pickup (guess)
  python tools/jetsim.py gates             mid-jet: a mech appears / a hit lands / the rule crashes -> off at once
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import io
import json
import math
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot as botmod  # noqa: E402
import loot  # noqa: E402
import nav  # noqa: E402

DT = 0.05
HASH = 8.0


def load_hello():
    with gzip.open(os.path.join(ROOT, "check", "stream.jsonl.gz"), "rt", encoding="utf-8") as fh:
        msgs = [json.loads(line) for line in fh if line.strip()]
    hello = dict(next(m for m in msgs if m.get("type") == "hello"))
    hello["map"] = dict(hello["map"], dir=os.path.join(ROOT, "arena"))
    return hello, [m for m in msgs if m.get("type") == "obs"]


class World:
    """Exact prisms with a spatial hash, plus the terrain from nav.Grid."""

    def __init__(self, grid):
        with open(os.path.join(ROOT, "arena", "v2", "world.json"), encoding="utf-8") as fh:
            w = json.load(fh)
        self.g = grid
        self.prisms = []
        self.hash = {}
        for p in w["prisms"]:
            ring = p.get("ring") or []
            if len(ring) < 6:
                continue
            xs, ys = ring[0::2], ring[1::2]
            q = {"ring": ring, "holes": [h for h in (p.get("holes") or []) if len(h) >= 6],
                 "base": float(p.get("base", 0.0)), "top": float(p.get("top", 0.0)), "kind": p.get("kind"),
                 "bb": (min(xs), min(ys), max(xs), max(ys))}
            k = len(self.prisms)
            self.prisms.append(q)
            for i in range(int(math.floor(q["bb"][0] / HASH)), int(math.floor(q["bb"][2] / HASH)) + 1):
                for j in range(int(math.floor(q["bb"][1] / HASH)), int(math.floor(q["bb"][3] / HASH)) + 1):
                    self.hash.setdefault((i, j), []).append(k)

    def at(self, x, y):
        """Prisms whose footprint contains (x, y)."""
        out = []
        for k in self.hash.get((int(math.floor(x / HASH)), int(math.floor(y / HASH))), ()):
            q = self.prisms[k]
            b = q["bb"]
            if x < b[0] or x > b[2] or y < b[1] or y > b[3]:
                continue
            if nav._point_in_ring(x, y, q["ring"]) and not any(nav._point_in_ring(x, y, h) for h in q["holes"]):
                out.append(q)
        return out

    def ground(self, x, y):
        return self.g.terrain_h(x, y)

    def support(self, x, y, z, up=1.2):
        """Highest surface under (x, y) at or below z + up (terrain or a prism top)."""
        s = self.ground(x, y)
        for q in self.at(x, y):
            if q["top"] <= z + up and q["top"] > s:
                s = q["top"]
        return s

    def solid(self, x, y, lo, hi):
        """A prism occupies (x, y) somewhere between heights lo and hi."""
        return any(q["base"] < hi and q["top"] > lo for q in self.at(x, y))

    def los(self, a, b, step=1.0):
        d = math.dist(a, b)
        k = max(1, int(d / step))
        for s in range(1, k):
            f = s / k
            x, y, z = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)
            if z < self.ground(x, y) - 0.5:
                return False
            if any(q["base"] < z < q["top"] for q in self.at(x, y)):
                return False
        return True


RING = [(math.cos(2 * math.pi * k / 12), math.sin(2 * math.pi * k / 12)) for k in range(12)]


class Mech:
    def __init__(self, world, cfg, x, y, z=None, energy=100.0):
        self.w = world
        self.c = cfg
        self.x, self.y = x, y
        self.z = world.support(x, y, world.ground(x, y), up=1.2) if z is None else z
        self.vx = self.vy = self.vz = 0.0
        self.grounded = True
        self.energy = energy
        self.since_spend = 9.0
        self.aim = [0.0, 0.0]
        self.r = float(cfg.get("radius", 2.5))
        self.h = float(cfg.get("height", 9.0))

    def blocked_at(self, x, y, z):
        """Our cylinder at (x, y) with feet at z would overlap a wall (anything we cannot step up onto)."""
        hi = z + self.h
        lo = z + float(self.c.get("step_up", 1.2))
        if self.w.solid(x, y, lo, hi):
            return True
        for cx, cy in RING:
            if self.w.solid(x + cx * self.r, y + cy * self.r, lo, hi):
                return True
        return False

    def step(self, move, boost, jump):
        c = self.c
        mx, my = (move or [0.0, 0.0])[:2]
        n = math.hypot(mx, my)
        if n > 1.0:
            mx, my = mx / n, my / n
        spend = False
        if self.grounded:
            speed = float(c["boost_speed"]) if (boost and n > 0.1 and self.energy > 0) else float(c["walk_speed"])
            if boost and n > 0.1 and self.energy > 0:
                self.energy -= float(c["boost_drain"]) * DT
                spend = True
            acc = float(c["ground_accel"])
        else:
            speed = float(c["walk_speed"])            # GUESS: air control aims at walk speed
            acc = float(c["air_accel"])
        tvx, tvy = mx * speed, my * speed
        dvx, dvy = tvx - self.vx, tvy - self.vy
        dv = math.hypot(dvx, dvy)
        if dv > acc * DT:
            dvx, dvy = dvx * acc * DT / dv, dvy * acc * DT / dv
        self.vx += dvx
        self.vy += dvy
        # vertical
        if jump and self.grounded:
            self.vz = float(c["jump_speed"])
            self.grounded = False
        if not self.grounded:
            self.vz -= float(c["gravity"]) * DT
            if boost and self.energy > 0:
                cap = float(c["max_rise_speed"])
                if self.vz < cap:
                    self.vz = min(self.vz + float(c["jet_accel"]) * DT, cap)
                self.energy -= float(c["jet_drain"]) * DT
                spend = True
        self.energy = max(0.0, self.energy)
        if spend:
            self.since_spend = 0.0
        else:
            self.since_spend += DT
            if self.since_spend >= float(c["energy_regen_delay"]):
                self.energy = min(float(c["energy"]), self.energy + float(c["energy_regen"]) * DT)
        # horizontal move with sliding
        nx_, ny_ = self.x + self.vx * DT, self.y + self.vy * DT
        if not self.blocked_at(nx_, ny_, self.z):
            self.x, self.y = nx_, ny_
        elif not self.blocked_at(nx_, self.y, self.z):
            self.x = nx_
            self.vy = 0.0
        elif not self.blocked_at(self.x, ny_, self.z):
            self.y = ny_
            self.vx = 0.0
        else:
            self.vx = self.vy = 0.0
        # vertical move: ceilings, landing (centre, then roof edges under the ring)
        nz = self.z + self.vz * DT
        if self.vz > 0:
            for px, py in [(self.x, self.y)] + [(self.x + a * self.r, self.y + b * self.r) for a, b in RING]:
                for q in self.w.at(px, py):
                    if q["base"] >= self.z + self.h - 0.01 and q["base"] < nz + self.h:
                        nz = min(nz, q["base"] - self.h)
                        self.vz = 0.0
        sup = self.w.support(self.x, self.y, self.z, up=1.2 if self.grounded else 0.05)
        if not self.grounded:
            edge = -1e9
            for a, b in RING:
                for q in self.w.at(self.x + a * self.r, self.y + b * self.r):
                    if nz <= q["top"] <= self.z + 0.05:
                        edge = max(edge, q["top"])
            sup = max(sup, edge)
        if self.grounded:
            if sup >= self.z - 1.2:
                self.z = sup
                self.vz = 0.0
            else:
                self.grounded = False
                self.z = nz if nz > sup else sup
        else:
            if nz <= sup:
                self.z = sup
                self.vz = 0.0
                self.grounded = True
            else:
                self.z = nz
        # on an edge with the centre off the roof (protocol 7, roof edges): moving toward the roof keeps us on the
        # edge (the next horizontal step carries us onto it); otherwise we are slid off, up to 0.6 m per tick
        if self.grounded:
            centre = self.w.support(self.x, self.y, self.z, up=0.05)
            if centre < self.z - 1.2:
                held = [(a, b) for a, b in RING
                        if any(abs(q["top"] - self.z) < 0.05 for q in self.w.at(self.x + a * self.r, self.y + b * self.r))]
                if not held:
                    self.grounded = False
                else:
                    hx = sum(a for a, _ in held) / len(held)
                    hy = sum(b for _, b in held) / len(held)
                    if mx * hx + my * hy <= 0.05:
                        hn = math.hypot(hx, hy) or 1.0
                        self.x -= 0.6 * hx / hn
                        self.y -= 0.6 * hy / hn


def visible(world, me, aim_yaw, p):
    """Our vision model (protocol 8): eye at z+8 sees the pickup centre or top; 45 m any way, 200 m in 120 deg."""
    ex, ey, ez = me.x, me.y, me.z + 8.0
    px, py, pz = p["pos"]
    d = math.hypot(px - ex, py - ey)
    if d > 200.0:
        return False
    if d > 45.0:
        b = math.atan2(py - ey, px - ex)
        if abs(botmod.wrap(b - aim_yaw)) > math.radians(60):
            return False
    return world.los((ex, ey, ez), (px, py, pz + 0.4)) or world.los((ex, ey, ez), (px, py, pz + 0.8))


def make_obs(t, tick, m, zone, pickups_vis, events, weapons, cubes=0):
    return {"type": "obs", "tick": tick, "t": t, "alive": 16,
            "self": {"id": 3, "pos": [m.x, m.y, m.z], "vel": [m.vx, m.vy, m.vz], "aim": list(m.aim),
                     "hp": 1400, "max_hp": 1400, "energy": m.energy, "max_energy": 100, "grounded": m.grounded,
                     "cubes": cubes, "upgrades": [], "in_zone": True, "weapons": weapons,
                     "stats": {"walk_speed": 11.7, "muzzle_height": 7.0}},
            "mechs": [], "projectiles": [], "crates": [], "cubes": [], "pickups": pickups_vis, "sounds": [],
            "beams": [], "events": events, "drops": [], "zone": zone, "offer": None}


def run(bot, world, cfg, start, pickups, zone, t_end=40.0, vis_model=True, trace=False, inject=None, obs_hook=None):
    """Closed loop: the bot drives a simulated mech. Returns a result dict."""
    m = Mech(world, cfg, start[0], start[1])
    gun = {"kind": "gun", "ammo": 12, "reload": 0, "cooldown": 0,
           "stats": {"damage": 45, "mag": 12, "range": 230, "speed": 252}}
    weapons = [gun, None]
    pickups = [dict(p) for p in pickups]
    events = []
    t = 0.0
    tick = 0
    res = {"took": None, "t_took": None, "t_back_ground": None, "max_z": m.z, "energy_min": m.energy,
           "trace": [], "roof_s": 0.0, "ms_max": 0.0, "answered": 0, "ticks": 0}
    ground0 = m.z
    while t < t_end:
        if inject:
            inject(t, m, pickups)
        vis = [p for p in pickups if (not vis_model) or visible(world, m, m.aim[0], p)]
        o = make_obs(t, tick, m, zone, [dict(p) for p in vis], events, weapons)
        events = []
        if obs_hook:
            obs_hook(t, o, bot)
        t0 = time.perf_counter()
        with contextlib.redirect_stderr(io.StringIO()):
            a = bot.act(o)
        res["ms_max"] = max(res["ms_max"], (time.perf_counter() - t0) * 1000)
        res["ticks"] += 1
        res["answered"] += 1 if a.get("tick") == o["tick"] else 0
        if a.get("aim"):
            # GUESS: torso turns at turn_rate 4 rad/s toward the commanded aim
            dy = botmod.wrap(a["aim"][0] - m.aim[0])
            m.aim[0] = botmod.wrap(m.aim[0] + max(-0.4, min(0.4, dy)))
            m.aim[1] = a["aim"][1]
        if a.get("take") is not None:
            p = next((p for p in pickups if p["id"] == a["take"]), None)
            if p is not None and math.hypot(p["pos"][0] - m.x, p["pos"][1] - m.y) <= 4.0 and abs(p["pos"][2] - m.z) <= 4.0:
                pickups.remove(p)
                weapons = [gun, {"kind": p["kind"], "cooldown": 1.65, "charge": 0}]
                events.append({"type": "took", "id": p["id"], "kind": p["kind"], "slot": 1})
                res["took"] = p["id"]
                res["t_took"] = t
            else:
                events.append({"type": "take_failed", "id": a["take"], "reason": "range" if p else "gone"})
        for k in range(2):
            m.step(a.get("move"), bool(a.get("boost")), bool(a.get("jump")) and k == 0)
        t += 0.1
        tick += 2
        res["max_z"] = max(res["max_z"], m.z)
        res["energy_min"] = min(res["energy_min"], m.energy)
        on_roof = m.z > world.ground(m.x, m.y) + 1.5 and m.grounded
        if on_roof:
            res["roof_s"] += 0.1
        if res["took"] is not None and res["t_back_ground"] is None and m.grounded and not on_roof:
            res["t_back_ground"] = t
        if trace:
            ro = bot.roofer
            res["trace"].append((round(t, 1), round(m.x, 1), round(m.y, 1), round(m.z, 1), m.grounded,
                                 round(m.energy), ro.phase if ro else "-", bool(a.get("jump")), bool(a.get("boost"))))
    res["end_z"] = m.z
    res["end_on_roof"] = m.grounded and m.z > world.ground(m.x, m.y) + 1.5
    res["log"] = list(bot.roofer.log) if bot.roofer else []
    res["tries"] = bot.roofer.tries if bot.roofer else 0
    res["fails"] = bot.roofer.fails if bot.roofer else 0
    return res


def static_zone(cx=0.0, cy=0.0):
    return {"center": [cx, cy], "radius": 2000, "next_center": [cx, cy], "next_radius": 1500,
            "shrink_start": 1e9, "shrink_end": 1e9 + 1, "dps": 20}


def fresh_bot(proto):
    b = botmod.Bot.__new__(botmod.Bot)
    b.__dict__.update(proto.__dict__)
    b.field_key = None
    b.field = None
    b.hist = []
    b.unstick_until = -1.0
    b.attacker = None
    b.attack_t = -99.0
    b.looter = loot.Looter(proto.grid, 4.0)
    b.cuber = loot.CubeHunter(proto.grid)
    b.roofer = loot.RoofLooter(proto.grid, 4.0)
    return b


# ------------------------------------------------------------------------------------------- commands
def cmd_validate(world, cfg, proto):
    print("validate: straight-up jet from flat open ground, 100 energy, boost held")
    for x, y in ((0.0, -300.0), (-200.0, 300.0), (100.0, 100.0)):
        m = Mech(world, cfg, x, y)
        if world.at(x, y):
            continue
        z0 = m.z
        t = 0.0
        jumped = False
        top = z0
        while t < 12:
            m.step([0, 0], True, not jumped)
            jumped = True
            top = max(top, m.z)
            t += DT
            if m.grounded and t > 1:
                break
        print(f"  from ({x:.0f},{y:.0f}): climbed {top - z0:.1f} m (protocol: 'about 60 m with 100 energy')")
    # walking into a wall stops us; jetting up it then moving over the top lands us on the roof
    print("validate: wall + roof landing on building 26 (top 12.81)")
    m = Mech(world, cfg, 330.0, -250.0)
    print(f"  start z {m.z:.1f} grounded {m.grounded} blocked {m.blocked_at(m.x, m.y, m.z)}")


def cmd_recorded(world, cfg, proto, hello, stream):
    zone = stream[0]["zone"]
    start = stream[0]["self"]["pos"]
    picks = {}
    for o in stream:
        for p in o.get("pickups") or []:
            picks.setdefault(p["id"], p)
    print(f"recorded match: spawn ({start[0]:.1f},{start[1]:.1f}), zone r={zone['radius']}, "
          "no mechs (the real match had mech 10 in view from 4.9 s: the rule would be OFF there)")
    for pid in (8007, 8001, 8006, 8003):
        p = picks[pid]
        for vis_model in (False, True):
            b = fresh_bot(proto)
            res = run(b, world, cfg, start, [p], zone, t_end=40.0, vis_model=vis_model)
            print(f"  {pid} {p['kind']:9s} roof {p['pos'][2]:.1f} m, {math.hypot(p['pos'][0]-start[0], p['pos'][1]-start[1]):.0f} m away,"
                  f" {'seen only when our model says visible' if vis_model else 'always listed (tests the climb only)'}:"
                  f" took={res['took']} at {res['t_took']} s, back on ground {res['t_back_ground']}, tries {res['tries']},"
                  f" fails {res['fails']}, min energy {res['energy_min']:.0f}, end on roof {res['end_on_roof']}")
            for ev in res["log"]:
                print(f"      t={ev[0]:.1f} {ev[1]}")


def cmd_gates(world, cfg, proto, stream):
    """The rule must switch off at once: a mech appears, a hit lands, or the rule crashes, mid-jet (8007 case)."""
    zone = stream[0]["zone"]
    start = stream[0]["self"]["pos"]
    p = next(p for o in stream for p in (o.get("pickups") or []) if p["id"] == 8007)
    mech = {"id": 10, "kind": "mech", "pos": [start[0] + 150, start[1], 0.0], "vel": [0, 0, 0], "aim": [0, 0],
            "hp": 1400, "max_hp": 1400, "weapons": ["gun"]}

    def mech_from(t0, t1=1e9):
        def hook(t, o, b):
            if t0 <= t < t1:
                o["mechs"] = [dict(mech)]
        return hook

    def hit_at(t0):
        def hook(t, o, b):
            if abs(t - t0) < 0.05:
                o["events"] = list(o["events"]) + [{"type": "damaged", "amount": 45, "weapon": "gun",
                                                    "bearing": 0.0, "by": 10}]
        return hook

    def crash_at(t0):
        def hook(t, o, b):
            if abs(t - t0) < 0.05 and b.roofer is not None:
                def boom(*a, **k):
                    raise RuntimeError("injected")
                b.roofer.update = boom
        return hook

    cases = [("baseline (no gate event)", None), ("mech listed from t=2.0 (mid-jet) for good", mech_from(2.0)),
             ("mech listed t=2.0-4.0 only, then gone", mech_from(2.0, 4.0)), ("hit by mech 10 at t=2.0", hit_at(2.0)),
             ("rule crashes at t=2.0", crash_at(2.0)), ("mech listed from t=0 (before commit)", mech_from(0.0))]
    for name, hook in cases:
        b = fresh_bot(proto)
        res = run(b, world, cfg, start, [p], zone, t_end=30.0, vis_model=False, trace=True, obs_hook=hook)
        boosts_after = [r for r in res["trace"] if r[0] > 2.05 and r[8]]
        jets_while_gated = [r for r in res["trace"] if hook is not None and name.startswith("mech listed from t=2.0")
                            and r[0] > 2.05 and (r[6] == "jet" or r[7])]
        print(f"  {name}: answered {res['answered']}/{res['ticks']}, took {res['took']}, tries {res['tries']},"
              f" fails {res['fails']}, roofer {'disabled' if b.roofer is None else 'on'},"
              f" boost ticks after 2.0 s: {len(boosts_after)}, roof-jet ticks while gated: {len(jets_while_gated)},"
              f" max z {res['max_z']:.1f}, end z {res['end_z']:.1f}, end on roof {res['end_on_roof']}")
        for ev in res["log"]:
            print(f"      t={ev[0]:.1f} {ev[1]}")


def random_roof_pickup(rng, world, grid):
    bs = [q for q in world.prisms if q["kind"] == "building"]
    while True:
        q = rng.choice(bs)
        b = q["bb"]
        for _ in range(20):
            x, y = rng.uniform(b[0], b[2]), rng.uniform(b[1], b[3])
            tops = world.at(x, y)
            if not tops:
                continue
            top = max(t["top"] for t in tops)
            if top - world.ground(x, y) < 2.0:
                continue
            return {"id": 9000 + rng.randrange(100000), "kind": rng.choice(["laser", "lightning"]),
                    "pos": [x, y, top]}


def random_start(rng, grid, world, p, dmin=15.0, dmax=80.0):
    for _ in range(400):
        a = rng.uniform(0, 2 * math.pi)
        d = rng.uniform(dmin, dmax)
        x, y = p["pos"][0] + d * math.cos(a), p["pos"][1] + d * math.sin(a)
        c = grid.cell_of(x, y)
        if c < 0 or grid.blocked[c] or grid.near[c] or world.at(x, y):
            continue
        return (x, y)
    return None


def cmd_sweep(world, cfg, proto, n, seed, vis_model):
    rng = random.Random(seed)
    g = proto.grid
    stats = {"scen": 0, "commit": 0, "jump": 0, "took": 0, "fail": {}, "t_took": [], "t_back": [],
             "end_on_roof": 0, "no_plan": 0, "roof_s": [], "climbs_ok": [], "climbs_fail": []}
    worst = []
    ms_max = 0.0
    for k in range(n):
        p = random_roof_pickup(rng, world, g)
        s = random_start(rng, g, world, p)
        if s is None:
            continue
        stats["scen"] += 1
        b = fresh_bot(proto)
        res = run(b, world, cfg, s, [p], static_zone(p["pos"][0], p["pos"][1]), t_end=45.0, vis_model=vis_model)
        ms_max = max(ms_max, res["ms_max"])
        log = res["log"]
        if any("commit" in e[1] for e in log):
            stats["commit"] += 1
        else:
            continue
        climb = next(float(e[1].split("climb ")[1].split()[0]) for e in log if "commit" in e[1])
        if res["tries"]:
            stats["jump"] += 1
        if res["took"]:
            stats["took"] += 1
            stats["t_took"].append(res["t_took"])
            stats["climbs_ok"].append(climb)
            if res["t_back_ground"] is not None:
                stats["t_back"].append(res["t_back_ground"] - res["t_took"])
        for e in log:
            if e[1].startswith("fail"):
                why = e[1].split(": ", 1)[1].split(" (")[0]
                stats["fail"][why] = stats["fail"].get(why, 0) + 1
                stats["climbs_fail"].append(climb)
        if res["end_on_roof"]:
            stats["end_on_roof"] += 1
        stats["roof_s"].append(res["roof_s"])
        worst.append((res["roof_s"], k, [round(v, 1) for v in p["pos"]], [round(v, 1) for v in s], log[-3:]))
    st = stats
    med = lambda v: sorted(v)[len(v) // 2] if v else None
    print(f"sweep (seed {seed}, visibility model {'on' if vis_model else 'off'}): {st['scen']} scenarios,"
          f" committed {st['commit']}, jumped {st['jump']}, took {st['took']}; slowest bot answer {ms_max:.2f} ms")
    print(f"  fails by reason: {st['fail']}")
    print(f"  time to take: median {med(st['t_took'])} s, max {max(st['t_took']) if st['t_took'] else None} s")
    print(f"  take -> back on the ground: median {med(st['t_back'])} s, max {max(st['t_back']) if st['t_back'] else None} s"
          f" ({len(st['t_back'])} of {st['took']} back down within the run)")
    print(f"  ended the 45 s run still on a roof: {st['end_on_roof']}; time on roofs: median {med(st['roof_s'])},"
          f" max {max(st['roof_s']) if st['roof_s'] else None} s")
    print(f"  climbs that worked: median {med(st['climbs_ok'])} m; climbs that failed: {sorted(st['climbs_fail'])}")
    worst.sort(reverse=True)
    for w in worst[:3]:
        print(f"  longest on a roof: {w[0]:.1f} s, scenario {w[1]}, pickup {w[2]}, start {w[3]}, log tail {w[4]}")


def cmd_freq(world, cfg, proto, n, seed):
    """GUESS-level: how often does a spawn-like spot have a qualifying, visible rooftop pickup within 80 m?

    Spawns: random open cells within 0.8 x the start radius of the recorded zone centre. Rooftop pickups per match:
    40 floor weapons x 1/4 on roofs x 2/3 laser or lightning = ~6.7, placed uniformly over roof AREA (GUESS: the
    protocol says only 'open ground and roofs (a quarter on roofs)'). Visible = our vision model from the spot with
    a 360 deg scan (the bot sweeps its view when it stands still, and looks where it walks)."""
    rng = random.Random(seed)
    g = proto.grid
    roof_cells = [c for c in range(g.n) if g.blocked[c] and g.top[c] - g.ground[c] >= 2.0]
    print(f"roof cells (4 m, >= 2 m high): {len(roof_cells)}")
    per = 40 * 0.25 * (2.0 / 3.0)
    hits = []
    near_cnt = []
    for k in range(n):
        while True:
            x = 41.3 + rng.uniform(-448, 448)
            y = -72.8 + rng.uniform(-448, 448)
            c = g.cell_of(x, y)
            if c >= 0 and not g.blocked[c] and math.hypot(x - 41.3, y + 72.8) < 448:
                break
        m = Mech(world, cfg, x, y)
        cand = [cc for cc in roof_cells if math.hypot(g.center(cc)[0] - x, g.center(cc)[1] - y) <= 80.0]
        near_cnt.append(len(cand))
        q = 0
        samples = cand if len(cand) <= 60 else rng.sample(cand, 60)
        for cc in samples:
            cx, cy = g.center(cc)
            p = {"id": 1, "kind": "laser", "pos": [cx, cy, g.top[cc]]}
            pl = loot.RoofLooter(g).plan(1, cx, cy, g.top[cc])
            if pl is None:
                continue
            if not (world.los((x, y, m.z + 8), (cx, cy, g.top[cc] + 0.4)) or
                    world.los((x, y, m.z + 8), (cx, cy, g.top[cc] + 0.8))):
                continue
            tos = sorted(pl["takeoffs"], key=lambda t: math.hypot(t[0] - x, t[1] - y))[:12]
            if any(math.hypot(t[0] - x, t[1] - y) <= 3.0 or g.line_walkable(x, y, t[0], t[1]) for t in tos):
                q += 1
        frac = q / len(samples) if samples else 0.0
        # expected qualifying pickups near this spot = per-match roof pickups x share of roof area that is near
        # AND qualifying
        hits.append(per * len(cand) / len(roof_cells) * frac)
    import statistics
    p_any = statistics.mean(1 - math.exp(-h) for h in hits)
    print(f"spawn-like spots: {n}; roof cells within 80 m: median {sorted(near_cnt)[n // 2]}")
    print(f"expected qualifying + visible rooftop laser/lightning within 80 m of the spot: mean {statistics.mean(hits):.3f}")
    print(f"P(at least one) from that spot (Poisson): {p_any:.1%}  [GUESS-level; before the no-mech gate and energy]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["validate", "recorded", "sweep", "freq", "gates"])
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--novis", action="store_true", help="sweep: list the pickup every tick (tests the climb only)")
    args = ap.parse_args()
    hello, stream = load_hello()
    with contextlib.redirect_stderr(io.StringIO()):
        proto = botmod.Bot(hello)
    world = World(proto.grid)
    cfg = hello["config"]["mech"]
    if args.cmd == "validate":
        cmd_validate(world, cfg, proto)
    elif args.cmd == "recorded":
        cmd_recorded(world, cfg, proto, hello, stream)
    elif args.cmd == "gates":
        cmd_gates(world, cfg, proto, stream)
    elif args.cmd == "sweep":
        cmd_sweep(world, cfg, proto, args.n, args.seed, not args.novis)
    else:
        cmd_freq(world, cfg, proto, args.n, args.seed)


if __name__ == "__main__":
    main()

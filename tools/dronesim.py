"""sub-12 scenario: one drone at 50 m on open ground, nobody else around. Runs the real Bot.act() in a small closed loop.

  python tools/dronesim.py [--runs 20] [--dist 50] [--cubes 0] [--why]

GUESS-LEVEL physics (the kit has no simulator): instant acceleration at walk speed; aim turns at turn_rate /
pitch_rate; gun 0.24 s cooldown, mag 12, reload 2.4 s (auto on empty), Gaussian spread sigma = spread_deg on yaw and
pitch, bullets fly straight at 252 m/s for 230 m and hit a cylinder (radius + 0.3 m bullet radius). The drone wanders
within 30 m of home at 5 m/s; it aggroes when hit or when we are within 45 m with line of sight while it is within 30 m
of home, then chases until we are 90 m away or it is 60 m from home, firing every 1 s at our current position (no lead:
GUESS) with sigma 2.5 deg, 90 m/s, 25 damage. Cubes drop where it dies (2, 0.5 m apart) and are picked up within
3.5 m. The first cube brings an offer of 3 upgrades drawn from the gun-only pool (seeded); the bot's pick is applied.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import bot as botmod  # noqa: E402
import loot  # noqa: E402
import nav  # noqa: E402
import stress  # noqa: E402

DT = 0.05          # server tick
GUN_POOL = ["plating", "armor", "servos", "capacitor", "reactor", "jets", "nanites", "gyros", "sensors",
            "gun_damage", "gun_rate", "gun_velocity", "gun_mag"]


def find_spot(grid, zone, dist):
    """An open-ground start spot near the next circle's centre with a clear, walkable, open 'dist' m line."""
    cx, cy = zone["next_center"]
    best = None
    for r in range(0, 260, 8):
        for k in range(24):
            a0 = 2 * math.pi * k / 24
            x, y = cx + r * math.cos(a0), cy + r * math.sin(a0)
            for j in range(16):
                a = 2 * math.pi * j / 16
                ok = True
                for s in range(0, int(dist + 12), 3):
                    for off in (-8, 0, 8):
                        px = x + s * math.cos(a) - off * math.sin(a)
                        py = y + s * math.sin(a) + off * math.cos(a)
                        c = grid.cell_of(px, py)
                        if c < 0 or grid.blocked[c] or abs(grid.surf[c] - grid.terrain_h(x, y)) > 1.5:
                            ok = False
                            break
                    if not ok:
                        break
                if ok:
                    return x, y, a
    return best


def seg_hits_cyl(p0, p1, c, rad, z0, z1):
    """Does the segment p0->p1 pass through the vertical cylinder at c (x, y), radius rad, z in [z0, z1]?"""
    n = 8
    for i in range(n + 1):
        f = i / n
        x = p0[0] + (p1[0] - p0[0]) * f
        y = p0[1] + (p1[1] - p0[1]) * f
        z = p0[2] + (p1[2] - p0[2]) * f
        if math.hypot(x - c[0], y - c[1]) <= rad and z0 <= z <= z1:
            return True
    return False


def run(seed, dist, cubes0, why):
    rng = random.Random(seed)
    hello, stream = stress.load_stream()
    tmpl = stream[-1]
    m = hello["map"]
    with contextlib.redirect_stderr(io.StringIO()):
        b = botmod.Bot(hello)
    grid = b.grid
    zone = dict(tmpl["zone"])
    spot = find_spot(grid, zone, dist)
    if spot is None:
        print("no open spot found")
        sys.exit(1)
    x, y, a = spot
    z = grid.surf[grid.cell_of(x, y)]
    cfg = hello["config"]
    npc = cfg["npc"]
    gcfg = dict(cfg["weapons"]["gun"])
    home = (x + dist * math.cos(a), y + dist * math.sin(a))
    dr = {"pos": [home[0], home[1], grid.surf[grid.cell_of(*home)]], "hp": 350.0, "aggro": False, "cd": 0.0,
          "wp": home, "vel": [0.0, 0.0]}
    me = {"pos": [x, y, z], "yaw": a + rng.uniform(-1.5, 1.5), "pitch": 0.0, "hp": 1400.0 * (1 + 0.04 * cubes0),
          "cubes": cubes0, "ammo": 12, "reload": 0.0, "cd": 0.0, "mag": 12, "reload_s": 2.4, "ups": []}
    t = float(tmpl["t"])
    tick = int(tmpl["tick"])
    bullets, dbullets, ground = [], [], []
    events = []
    offer = None
    picks = []
    log = {"first_shot": None, "kill": None, "shots": 0, "hits": 0, "dmg_taken": 0.0, "cube_t": [], "max_ms": 0.0,
           "active": 0, "min_d": 1e9, "held_d": []}
    t0 = t
    move = (0.0, 0.0)
    act = {}
    for step in range(int(40 / DT)):
        # ---- decision every 2 server ticks
        if step % 2 == 0:
            mx = 1400.0 * (1 + 0.04 * min(me["cubes"], 10))
            for u in me["ups"]:
                if u == "plating":
                    mx *= 1.15
            o = json.loads(json.dumps(tmpl))
            o["tick"], o["t"] = tick, t
            s = o["self"]
            s["pos"] = list(me["pos"])
            s["vel"] = [move[0] * 11.7, move[1] * 11.7, 0.0]
            s["aim"] = [me["yaw"], me["pitch"]]
            s["hp"], s["max_hp"] = me["hp"], mx
            s["grounded"], s["in_zone"], s["energy"] = True, True, 100.0
            s["cubes"], s["upgrades"] = me["cubes"], list(me["ups"])
            st = dict(gcfg)
            st["mag"], st["reload"] = me["mag"], me["reload_s"]
            s["weapons"] = [{"kind": "gun", "cooldown": max(me["cd"], 0.0), "stats": st, "ammo": me["ammo"],
                             "reload": max(me["reload"], 0.0)}, None]
            o["zone"] = zone
            o["mechs"] = []
            if dr["hp"] > 0:
                o["mechs"] = [{"id": 104, "kind": "npc", "pos": list(dr["pos"]), "vel": [dr["vel"][0], dr["vel"][1], 0],
                               "aim": [0, 0], "hp": dr["hp"], "max_hp": 350}]
            o["cubes"] = [{"id": 5000 + i, "pos": list(p)} for i, p in enumerate(ground)]
            o["crates"], o["pickups"], o["projectiles"], o["drops"], o["sounds"], o["beams"] = [], [], [], [], [], []
            o["events"] = events
            o["offer"] = offer
            events = []
            t1 = time.perf_counter()
            with contextlib.redirect_stderr(io.StringIO()):
                act = b.act(o)
            log["max_ms"] = max(log["max_ms"], (time.perf_counter() - t1) * 1000)
            if b.drone_pick is not None:
                log["active"] += 1
                log["held_d"].append(math.hypot(dr["pos"][0] - me["pos"][0], dr["pos"][1] - me["pos"][1]))
            elif why and dr["hp"] > 0 and step < 40:
                print(f"  t={t - t0:4.1f} rule off; d={math.hypot(dr['pos'][0] - me['pos'][0], dr['pos'][1] - me['pos'][1]):.1f}")
            mv = act.get("move") or [0, 0]
            nrm = math.hypot(mv[0], mv[1])
            move = (mv[0] / nrm, mv[1] / nrm) if nrm > 1 else (mv[0], mv[1])
            if offer is not None and act.get("pick") is not None:
                up = offer["choices"][int(act["pick"])]["id"]
                picks.append((round(t - t0, 2), [c["id"] for c in offer["choices"]], up))
                me["ups"].append(up)
                if up == "plating":
                    me["hp"] *= 1.15
                if up == "gun_mag":
                    me["mag"] += 8
                    me["reload_s"] *= 0.8
                offer = None
            if act.get("reload") and me["reload"] <= 0 and me["ammo"] < me["mag"]:
                me["reload"] = me["reload_s"]
        # ---- physics, one server tick
        tick += 1
        t += DT
        nx = me["pos"][0] + move[0] * 11.7 * DT
        ny = me["pos"][1] + move[1] * 11.7 * DT
        c = grid.cell_of(nx, ny)
        if c >= 0 and not grid.blocked[c]:
            me["pos"] = [nx, ny, grid.surf[c]]
        aim = act.get("aim")
        if aim:
            dy = botmod.wrap(aim[0] - me["yaw"])
            me["yaw"] = botmod.wrap(me["yaw"] + max(-4 * DT, min(4 * DT, dy)))
            me["pitch"] += max(-3 * DT, min(3 * DT, aim[1] - me["pitch"]))
        me["cd"] -= DT
        if me["reload"] > 0:
            me["reload"] -= DT
            if me["reload"] <= 0:
                me["ammo"] = me["mag"]
        fire = act.get("fire") or [False, False]
        if fire[0] and me["cd"] <= 0 and me["ammo"] > 0 and me["reload"] <= 0:
            yaw = me["yaw"] + math.radians(rng.gauss(0, gcfg["spread_deg"]))
            pit = me["pitch"] + math.radians(rng.gauss(0, gcfg["spread_deg"]))
            v = (math.cos(pit) * math.cos(yaw) * 252, math.cos(pit) * math.sin(yaw) * 252, math.sin(pit) * 252)
            mz = 7.0 * (1 + 0.05 * me["cubes"])
            bullets.append([[me["pos"][0], me["pos"][1], me["pos"][2] + mz], v, 0.0])
            me["ammo"] -= 1
            me["cd"] = gcfg["cooldown"]
            log["shots"] += 1
            if log["first_shot"] is None:
                log["first_shot"] = t - t0
            if me["ammo"] == 0:
                me["reload"] = me["reload_s"]
        # our bullets
        alive = []
        for bl in bullets:
            p0 = bl[0]
            p1 = [p0[i] + bl[1][i] * DT for i in range(3)]
            bl[2] += 252 * DT
            if dr["hp"] > 0 and seg_hits_cyl(p0, p1, dr["pos"], npc["radius"] + 0.3, dr["pos"][2] - 0.3,
                                             dr["pos"][2] + npc["height"] + 0.3):
                dmg = gcfg["damage"] * (1 + 0.04 * min(me["cubes"], 10))
                if "gun_damage" in me["ups"]:
                    dmg *= 1.15
                dr["hp"] -= dmg
                dr["aggro"] = True
                log["hits"] += 1
                events.append({"type": "hit", "target": 104, "amount": dmg, "weapon": "gun"})
                if dr["hp"] <= 0:
                    log["kill"] = t - t0
                    ground += [[dr["pos"][0] + 0.25, dr["pos"][1], dr["pos"][2]],
                               [dr["pos"][0] - 0.25, dr["pos"][1], dr["pos"][2]]]
                continue
            if bl[2] < 230 and grid.los(p0, p1):
                bl[0] = p1
                alive.append(bl)
        bullets = alive
        # drone
        if dr["hp"] > 0:
            ddx, ddy = me["pos"][0] - dr["pos"][0], me["pos"][1] - dr["pos"][1]
            d = math.hypot(ddx, ddy)
            log["min_d"] = min(log["min_d"], d)
            from_home = math.hypot(dr["pos"][0] - home[0], dr["pos"][1] - home[1])
            eye = (dr["pos"][0], dr["pos"][1], dr["pos"][2] + 2.5)
            sees = grid.los(eye, (me["pos"][0], me["pos"][1], me["pos"][2] + 4.5))
            if not dr["aggro"] and d < npc["aggro_range"] and sees and from_home < npc["leash"]:
                dr["aggro"] = True
            if dr["aggro"] and (d > npc["deaggro_range"] or from_home > npc["chase_leash"]):
                dr["aggro"] = False
            if dr["aggro"]:
                tgt = me["pos"]
            else:
                if math.hypot(dr["wp"][0] - dr["pos"][0], dr["wp"][1] - dr["pos"][1]) < 1.0 or rng.random() < 0.01:
                    aa, rr = rng.uniform(-math.pi, math.pi), rng.uniform(0, npc["leash"])
                    dr["wp"] = (home[0] + rr * math.cos(aa), home[1] + rr * math.sin(aa))
                tgt = dr["wp"]
            vx, vy = tgt[0] - dr["pos"][0], tgt[1] - dr["pos"][1]
            n = math.hypot(vx, vy)
            if dr["aggro"] and n < 8:
                dr["vel"] = [0.0, 0.0]
            elif n > 0.01:
                dr["vel"] = [vx / n * npc["speed"], vy / n * npc["speed"]]
            c2 = grid.cell_of(dr["pos"][0] + dr["vel"][0] * DT, dr["pos"][1] + dr["vel"][1] * DT)
            if c2 >= 0 and not grid.blocked[c2]:
                dr["pos"] = [dr["pos"][0] + dr["vel"][0] * DT, dr["pos"][1] + dr["vel"][1] * DT, grid.surf[c2]]
            dr["cd"] -= DT
            if dr["aggro"] and dr["cd"] <= 0 and sees:
                yaw = math.atan2(ddy, ddx) + math.radians(rng.gauss(0, npc["spread_deg"]))
                tz = me["pos"][2] + 4.5 * (1 + 0.05 * me["cubes"])
                pit = math.atan2(tz - eye[2], max(d, 0.1)) + math.radians(rng.gauss(0, npc["spread_deg"]))
                sp = npc["projectile_speed"]
                dbullets.append([list(eye), (math.cos(pit) * math.cos(yaw) * sp, math.cos(pit) * math.sin(yaw) * sp,
                                             math.sin(pit) * sp), 0.0])
                dr["cd"] = npc["cooldown"]
        alive = []
        for bl in dbullets:
            p0 = bl[0]
            p1 = [p0[i] + bl[1][i] * DT for i in range(3)]
            bl[2] += npc["projectile_speed"] * DT
            sz = 1 + 0.05 * me["cubes"]
            if seg_hits_cyl(p0, p1, me["pos"], 2.5 * sz + 0.3, me["pos"][2] - 0.3, me["pos"][2] + 9 * sz + 0.3):
                me["hp"] -= npc["damage"]
                log["dmg_taken"] += npc["damage"]
                events.append({"type": "damaged", "amount": npc["damage"], "weapon": "npc",
                               "bearing": math.atan2(p0[1] - me["pos"][1], p0[0] - me["pos"][0]), "by": 104})
                continue
            if bl[2] < 200 and grid.los(p0, p1):
                bl[0] = p1
                alive.append(bl)
        dbullets = alive
        # cube pickup
        keep = []
        for p in ground:
            if math.hypot(p[0] - me["pos"][0], p[1] - me["pos"][1]) <= 3.5:
                me["cubes"] += 1
                log["cube_t"].append(round(t - t0, 2))
                events.append({"type": "pickup", "cubes": me["cubes"]})
                if me["cubes"] in (1, 3, 6, 10):
                    pool = [u for u in GUN_POOL if me["ups"].count(u) < 2]
                    ch = rng.sample(pool, 3)
                    offer = {"id": tick, "expires": t + 5, "choices": [{"id": u, "name": u, "desc": u} for u in ch]}
            else:
                keep.append(p)
        ground = keep
        if dr["hp"] <= 0 and not ground and offer is None and len(log["cube_t"]) >= 2:
            log["end"] = t - t0
            break
    log["picks"] = picks
    log["hp_end"] = me["hp"]
    return log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--dist", type=float, default=50.0)
    ap.add_argument("--cubes", type=int, default=0)
    ap.add_argument("--why", action="store_true")
    args = ap.parse_args()
    rows = []
    for seed in range(1, args.runs + 1):
        r = run(seed, args.dist, args.cubes, args.why)
        rows.append(r)
        held = r["held_d"]
        print(f"seed {seed:2d}: rule active {r['active']:3d} decisions; first shot {r['first_shot']}; kill "
              f"{'-' if r['kill'] is None else f'{r[chr(107) + chr(105) + chr(108) + chr(108)]:.2f} s'}; shots "
              f"{r['shots']} hits {r['hits']}; damage taken {r['dmg_taken']:.0f}; closest {r['min_d']:.1f} m; "
              f"cubes at {r['cube_t']}; picks {r['picks']}; max tick {r['max_ms']:.1f} ms")
    kills = sorted(r["kill"] for r in rows if r["kill"] is not None)
    if kills:
        print(f"\nkilled {len(kills)}/{len(rows)}; time to kill median {kills[len(kills) // 2]:.2f} s, worst "
              f"{kills[-1]:.2f} s; both cubes {sum(1 for r in rows if len(r['cube_t']) >= 2)}/{len(rows)}; "
              f"median damage taken {sorted(r['dmg_taken'] for r in rows)[len(rows) // 2]:.0f}; "
              f"offers answered {sum(1 for r in rows if r['picks'])}/{len(rows)}")


if __name__ == "__main__":
    main()

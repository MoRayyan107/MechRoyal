"""Pick up a better weapon when one is close and it is safe to fetch it (sub-04).

The rule, in plain words:

* We only fetch a weapon that is better than what is in our free slot: laser (best)
  over lightning, over nothing. The gun stays in slot 0. Mortars are never fetched
  (hard to aim, our own splash hurts us, and carrying one invites laser cook-off).
* ON only when ALL of these hold:
  - no enemy mech is listed in obs.mechs (nobody sees us or is being tracked by us),
  - no mech has hit us in the last SAFE_HIT_S seconds,
  - we are inside the zone and the zone is not pressing (the caller checks its boost rule),
  - a candidate pickup exists: visible, within LOOT_M, on walkable ground (not on a roof),
    reachable by a straight walk, no drone seen within DRONE_M of it, and the zone leaves
    ZONE_SLACK_S seconds to spare after walking there and back to the goal circle.
* While ON we walk straight to the pickup at full speed; within reach we send `take`
  (with `drop: 1` when swapping lightning for a laser).
* We give up on a pickup for good if it takes more than twice the expected walk + 4 s,
  or the server says it is gone / we already have that kind / our slot is busy.
* OFF (return None) otherwise: the caller's normal (sub-01) behaviour runs unchanged.
"""
from __future__ import annotations

import math

LOOT_M = 80.0          # ~7 s walk (sub-05; sub-04 used 60 m). Other mechs spawn >= 130 m away
SAFE_HIT_S = 4.0
ZONE_SLACK_S = 20.0
DRONE_M = 45.0         # drones attack mechs within 45 m of them
ROOF_DZ = 2.5          # a pickup this far above the walking height is on a roof: skip it
RANK = {"laser": 3, "lightning": 2}


class Looter:
    def __init__(self, grid, reach: float = 4.0):
        self.g = grid
        self.reach = float(reach)
        self.target = None          # pickup id we are fetching
        self.spent = {}             # pickup id -> seconds actually spent fetching it (sub-05b fix #2)
        self.limit = {}             # pickup id -> time budget, set when we first go for it
        self.prev_t = None          # time of the previous tick on which we were fetching
        self.banned = set()         # pickup ids we gave up on
        self.state = "off"

    def read_events(self, obs) -> None:
        """Called by the bot on EVERY tick (sub-05b fix #6), so take results are never missed."""
        for ev in obs.get("events") or []:
            t = ev.get("type")
            if t == "take_failed" and ev.get("reason") in ("gone", "have", "full", "busy"):
                self.banned.add(ev.get("id"))
                if ev.get("id") == self.target:
                    self.target = None
            elif t == "took":
                self.banned.add(ev.get("id"))
                self.target = None

    def update(self, obs, me, now, attacker, attack_t, zone, walk, goal, t_out_fn, circle_fn):
        """None (rule off), or (yaw, pickup_id, take_now: bool, drop_slot or None)."""
        x, y, z = me["pos"]
        # --- safety gates
        if any(m.get("kind") == "mech" for m in obs.get("mechs") or []):
            return self._off("mech in view")
        if attacker is not None and isinstance(attacker, int) and attacker < 100 and now - attack_t < SAFE_HIT_S:
            return self._off("recently hit")
        if not me.get("in_zone", True):
            return self._off("outside zone")
        weapons = me.get("weapons") or []
        held = {w.get("kind") for w in weapons if w}
        free = weapons[1] if len(weapons) > 1 else None
        cur_rank = RANK.get(free.get("kind"), 1) if free else 0
        # (no "laser charging" check needed: with a laser in slot 1, cur_rank is the top rank,
        #  so no pickup can beat it and we never try to swap a laser out)
        drones = [m["pos"] for m in obs.get("mechs") or [] if m.get("kind") == "npc" and m.get("pos")]
        g = self.g
        gx, gy, gr = goal
        best = None
        for p in obs.get("pickups") or []:
            kind = p.get("kind")
            pid = p.get("id")
            if RANK.get(kind, 0) <= cur_rank or kind in held or pid in self.banned or not p.get("pos"):
                continue
            px, py, pz = p["pos"]
            d = math.hypot(px - x, py - y)
            if d > LOOT_M:
                continue
            c = g.cell_of(px, py)
            if c < 0 or g.blocked[c] or pz - g.surf[c] > ROOF_DZ:
                continue                                # on a roof / inside a building footprint
            if d > self.reach and not g.line_walkable(x, y, px, py):
                continue
            if any(math.hypot(px - a[0], py - a[1]) < DRONE_M for a in drones):
                continue
            there = d / max(walk, 1.0)
            back = max(math.hypot(gx - px, gy - py) - gr, 0.0) * 1.5 / max(walk, 1.0)
            if t_out_fn(zone, px, py, now) < there + back + ZONE_SLACK_S:
                continue
            cx, cy, cr = circle_fn(zone, now + there + 5.0)
            if math.hypot(px - cx, py - cy) > cr - 10.0:
                continue
            key = (-RANK[kind], d)
            if best is None or key < best[0]:
                best = (key, pid, px, py, pz, d, kind)
        if best is None:
            return self._off("nothing near")
        _, pid, px, py, pz, d, kind = best
        # give-up timer: count only the time we actually spend fetching this pickup, across any ticks
        # where the rule was off in between (a pickup blinking out of view no longer resets it)
        if pid not in self.limit:
            self.limit[pid] = 2.0 * d / max(walk, 1.0) + 4.0
        dt = 0.1 if self.prev_t is None else min(max(now - self.prev_t, 0.0), 0.2)
        self.spent[pid] = self.spent.get(pid, 0.0) + dt
        self.prev_t = now
        self.target = pid
        if self.spent[pid] > self.limit[pid]:
            self.banned.add(pid)
            return self._off("gave up")
        drop = 1 if free else None
        yaw = math.atan2(py - y, px - x)
        take_now = d <= self.reach - 0.5 and abs(pz - z) < 3.5
        self.state = "take" if take_now else "go"
        return (yaw, pid, take_now, drop)

    def _off(self, why: str):
        self.state = "off"
        self.target = None
        self.prev_t = None
        return None


# ----------------------------------------------------------------------------- sub-09: get cubes
CUBE_M = 60.0             # walk to a visible cube this close (~5 s walk)
CRATE_M = 60.0            # shoot a visible ground crate this close (its cube must be fetchable too)
CRATE_MIN_AMMO = 9        # start on a crate only with this many rounds (300 HP / 45 = 7 hits + misses)
CRATE_MAX = 4             # crates started per match, to cap the noise (gun 320 m, crate 220 m)
CRATE_GIVEUP_S = 6.0      # stop aiming at a crate that is still standing after this long
CRATE_RELOAD_BELOW = 6    # after a crate, reload early when the magazine is below this
CRATE_AIM_Z = 1.2         # crate centre above its pos (crate height 2.4 m)


class CubeHunter:
    """sub-09: collect power cubes, and break nearby ground crates for theirs.

    The rule, in plain words:
    * Same safety gates as the weapon rule: no enemy mech listed, no hit in the last SAFE_HIT_S seconds, inside
      the zone and the zone not pressing (the caller checks the last two). Weapons outrank cubes: the caller only
      asks for a cube when the weapon rule returned nothing.
    * A cube spot (or a crate, whose cube drops where it stands) is OK when it is within CUBE_M / CRATE_M, on open
      walkable ground (not a roof, not inside a footprint), reachable by a straight walk, no drone within DRONE_M of
      it, and the zone leaves time to walk there and back (same test as the weapon rule).
    * Cubes are collected automatically within 3.5 m, so we just walk onto the nearest OK cube. Give-up timer: the
      time actually spent on that cube, at most 2 x walk + 4 s.
    * Crates: aim the gun at the nearest OK ground crate with a clear line and fire while the legs keep walking the
      route. A new crate needs >= CRATE_MIN_AMMO rounds; at most CRATE_MAX per match; give up after CRATE_GIVEUP_S.
    * OFF (None) otherwise: the caller's sub-05b behaviour runs unchanged.
    """

    def __init__(self, grid):
        self.g = grid
        self.cube_spent = {}      # cube id -> seconds spent walking to it
        self.cube_limit = {}
        self.cube_prev_t = None
        self.cube_banned = set()
        self.crate_spent = {}     # crate id -> seconds spent aiming at it
        self.crate_prev_t = None
        self.crate_banned = set()
        self.crates_started = set()
        self.last_shot_t = -99.0  # last time we fired at a crate (early-reload rule)

    @staticmethod
    def gates_ok(obs, attacker, attack_t, now) -> bool:
        if any(m.get("kind") == "mech" for m in obs.get("mechs") or []):
            return False
        if attacker is not None and isinstance(attacker, int) and attacker < 100 and now - attack_t < SAFE_HIT_S:
            return False
        return True

    def spot_ok(self, px, py, pz, x, y, max_d, drones, zone, walk, goal, now, t_out_fn, circle_fn):
        """Distance to the spot if a cube there could be fetched safely, else None."""
        g = self.g
        d = math.hypot(px - x, py - y)
        if d > max_d:
            return None
        c = g.cell_of(px, py)
        if c < 0 or g.blocked[c] or pz - g.surf[c] > ROOF_DZ:
            return None                                  # roof or building footprint
        if d > 3.0 and not g.line_walkable(x, y, px, py):
            return None
        if any(math.hypot(px - a[0], py - a[1]) < DRONE_M for a in drones):
            return None
        gx, gy, gr = goal
        there = d / max(walk, 1.0)
        back = max(math.hypot(gx - px, gy - py) - gr, 0.0) * 1.5 / max(walk, 1.0)
        if t_out_fn(zone, px, py, now) < there + back + ZONE_SLACK_S:
            return None
        cx, cy, cr = circle_fn(zone, now + there + 5.0)
        if math.hypot(px - cx, py - cy) > cr - 10.0:
            return None
        return d

    def update_cube(self, obs, me, now, attacker, attack_t, zone, walk, goal, t_out_fn, circle_fn):
        """None (off) or the yaw to walk toward the chosen cube."""
        cubes = obs.get("cubes") or []
        if not cubes or not self.gates_ok(obs, attacker, attack_t, now) or not me.get("in_zone", True):
            self.cube_prev_t = None
            return None
        x, y = me["pos"][0], me["pos"][1]
        drones = [m["pos"] for m in obs.get("mechs") or [] if m.get("kind") == "npc" and m.get("pos")]
        best = None
        for cb in cubes:
            cid = cb.get("id")
            if cid in self.cube_banned or not cb.get("pos"):
                continue
            px, py, pz = cb["pos"]
            d = self.spot_ok(px, py, pz, x, y, CUBE_M, drones, zone, walk, goal, now, t_out_fn, circle_fn)
            if d is not None and (best is None or d < best[0]):
                best = (d, cid, px, py)
        if best is None:
            self.cube_prev_t = None
            return None
        d, cid, px, py = best
        if cid not in self.cube_limit:
            self.cube_limit[cid] = 2.0 * d / max(walk, 1.0) + 4.0
        dt = 0.1 if self.cube_prev_t is None else min(max(now - self.cube_prev_t, 0.0), 0.2)
        self.cube_spent[cid] = self.cube_spent.get(cid, 0.0) + dt
        self.cube_prev_t = now
        if self.cube_spent[cid] > self.cube_limit[cid]:
            self.cube_banned.add(cid)
            self.cube_prev_t = None
            return None
        return math.atan2(py - y, px - x)

    def pick_crate(self, obs, me, now, attacker, attack_t, zone_ok, zone, walk, goal, t_out_fn, circle_fn,
                   gun, muzzle):
        """None (off) or (aim point, crate id) of the crate to shoot this tick."""
        crates = obs.get("crates") or []
        if not crates or not zone_ok or gun is None or not self.gates_ok(obs, attacker, attack_t, now):
            self.crate_prev_t = None
            return None
        x, y = me["pos"][0], me["pos"][1]
        ammo = int(gun.get("ammo", 0) or 0)
        drones = [m["pos"] for m in obs.get("mechs") or [] if m.get("kind") == "npc" and m.get("pos")]
        g = self.g
        best = None
        for cr in crates:
            kid = cr.get("id")
            if kid in self.crate_banned or not cr.get("pos"):
                continue
            if kid not in self.crates_started and (len(self.crates_started) >= CRATE_MAX or ammo < CRATE_MIN_AMMO):
                continue
            px, py, pz = cr["pos"]
            d = self.spot_ok(px, py, pz, x, y, CRATE_M, drones, zone, walk, goal, now, t_out_fn, circle_fn)
            if d is None:
                continue
            aim_pt = (px, py, pz + CRATE_AIM_Z)
            if not g.los(muzzle, aim_pt):
                continue
            key = (kid not in self.crates_started, d)     # finish a crate we started before a new one
            if best is None or key < best[0]:
                best = (key, kid, aim_pt)
        if best is None:
            self.crate_prev_t = None
            return None
        _, kid, aim_pt = best
        dt = 0.1 if self.crate_prev_t is None else min(max(now - self.crate_prev_t, 0.0), 0.2)
        self.crate_spent[kid] = self.crate_spent.get(kid, 0.0) + dt
        self.crate_prev_t = now
        if self.crate_spent[kid] > CRATE_GIVEUP_S:
            self.crate_banned.add(kid)
            self.crate_prev_t = None
            return None
        return aim_pt, kid


# ----------------------------------------------------------------------------- sub-06: rooftop weapons
ROOF_M = LOOT_M           # same radius as ground weapons (80 m, horizontal, from us to the pickup)
ROOF_MATCH_DZ = 1.0       # a pickup is on a roof when its cell is a building whose roof is within 1 m of its z
ROOF_REGION_M = 30.0      # the roof = connected cells at that height within 30 m of the pickup
ROOF_MAX_CLIMB = 25.0     # roof height above the take-off ground
ROOF_MIN_CLIMB = 1.2      # below this it is a step, not a climb
ROOF_EXTRA_ZONE_S = 4.0   # extra seconds demanded from the zone check (on top of ZONE_SLACK_S)
ROOF_MAX_TRIES = 2        # jumps per match
ROOF_MAX_FAILS = 2        # failed jumps per match before the rule is off for good
ROOF_ARRIVE_M = 3.0       # start the jump this close to the take-off point (a wall may stop us short of it)
ROOF_CLEAR = 1.5          # jet until the feet are this far above the roof
ROOF_STALL_S = 1.5        # fail: no height gain for this long while still below roof + ROOF_CLEAR
ROOF_MIN_ENERGY = 8.0     # fail: energy below this while still below the roof
ROOF_BACKSTOP_S = 10.0    # on the roof this long after landing: walk back off over the take-off edge
ROOF_BACKSTOP_END_S = 25.0
ROOF_CLIMB_COST = 1.5     # metres of walking one metre of climb is worth, when comparing with a ground pickup


def roof_energy_need(climb: float) -> float:
    """Energy demanded before a jump (guess: jets cost ~1.5/m at 15/s and 10 m/s; this is ~1.3x that + 30)."""
    return 2.0 * climb + 30.0


class RoofLooter:
    """sub-06: fetch a laser / lightning lying on a low roof, by jump + jets.

    The rule, in plain words:
    * Candidate: a visible laser/lightning better than our slot 1, within ROOF_M, whose cell is a building whose
      roof height matches the pickup z (within 1 m). The roof = connected cells at that height within 30 m. A
      take-off point = an open cell next to that roof, at most 25 m below it, from which the straight line to the
      pickup stays on that roof the whole way (no higher section, no gap), and which we can reach by a straight
      walk. Energy >= 2 x climb + 30. No drone within 45 m of the pickup or the take-off. The zone check is the
      ground rule's plus 4 s, with the climb counted in the travel time.
    * Gates, checked every tick: no mech listed, no hit of any kind in the last SAFE_HIT_S, inside the zone and the
      zone not pressing (the caller does not call us otherwise). Any gate failing: the rule is off at once (None),
      and an attempt in the air is over (it still counts as one of the 2).
    * A ground pickup wins unless the rooftop one costs less: walk + roof walk + 1.5 x climb < ground distance.
    * Attempt: walk to the take-off point, then push toward the pickup, jump and jet until the feet are 1.5 m above
      the roof; then coast over it (jet again only if we sink below roof + 1.5 m before we are over it); walk
      to the pickup and `take` it. We remember where it lies while it is out of sight.
    * Failure (bans that pickup, counts toward ROOF_MAX_FAILS): no height gain for 1.5 s while below roof + 1.5 m,
      energy < 8 below the roof, back on the ground after jetting, or out of time (2 x walk + 8 s) after a jump.
      At most 2 jumps per match; 2 failures and the rule is off for the match.
    * Backstop: still on that roof 10 s after landing (and nothing else to do): walk back out over the take-off edge.
    * OFF (None) otherwise: the caller's sub-09 behaviour runs unchanged.
    """

    def __init__(self, grid, reach: float = 4.0):
        self.g = grid
        self.reach = float(reach)
        self.plans = {}           # pickup id -> plan dict or None (roof geometry is static: computed once)
        self.banned = set()
        self.tries = 0
        self.fails = 0
        self.cur = None           # the pickup we are committed to (dict), or None
        self.phase = "off"        # off / approach / jet / roof / down
        self.prev_t = None        # time of our previous active tick (a gap means the caller skipped us)
        self.landed = None        # dict for the backstop after we landed on a roof
        self.why = ""             # last reason, for logs and tests
        self.log = []             # (t, event) for tests

    # ---------------------------------------------------------------- geometry
    def plan(self, pid, px, py, pz):
        """Static roof plan for a pickup: {H, region, takeoffs[(tx, ty, climb, roof_d, ux, uy)]} or None."""
        key = (pid, round(px), round(py), round(pz))
        if key in self.plans:
            return self.plans[key]
        if len(self.plans) > 500:
            self.plans.clear()                             # never happens in a match (40 + 36 pickups); keeps memory flat
        g = self.g
        out = None
        c0 = g.cell_of(px, py)
        if c0 >= 0 and g.blocked[c0] and abs(g.top[c0] - pz) <= ROOF_MATCH_DZ:
            H = float(pz)
            region = {c0}
            todo = [c0]
            nx = g.nx
            while todo:
                c = todo.pop()
                for v in (c + 1, c - 1, c + nx, c - nx):
                    if v in region or v < 0 or v >= g.n or not g.blocked[v] or abs(g.top[v] - H) > ROOF_MATCH_DZ:
                        continue
                    vx, vy = g.center(v)
                    if math.hypot(vx - px, vy - py) > ROOF_REGION_M:
                        continue
                    region.add(v)
                    todo.append(v)
            takeoffs = []
            seen = set()
            for c in region:
                for v in (c + 1, c - 1, c + nx, c - nx):
                    if v in seen or v < 0 or v >= g.n or g.blocked[v]:
                        continue
                    seen.add(v)
                    # the next cell further out must be open ground too (>= 8 m of open ground: not a narrow gap
                    # between two roofs, which a 5 m wide mech bridges instead of stepping down into)
                    w2 = v + (v - c)
                    if w2 < 0 or w2 >= g.n or g.blocked[w2] or abs(g.surf[w2] - g.surf[v]) > 1.2:
                        continue
                    climb = H - g.surf[v]
                    if climb <= ROOF_MIN_CLIMB or climb > ROOF_MAX_CLIMB:
                        continue
                    tx, ty = g.center(v)
                    d = math.hypot(px - tx, py - ty)
                    if d < 0.5:
                        continue
                    # every 1 m sample from the take-off cell to the pickup: still the take-off cell, then this
                    # roof and nothing else (no gap, no higher or lower section)
                    k = max(1, int(d))
                    ok = True
                    entered = False
                    for s in range(1, k + 1):
                        f = s / k
                        cc = g.cell_of(tx + (px - tx) * f, ty + (py - ty) * f)
                        if cc in region:
                            entered = True
                        elif cc == v and not entered:
                            continue
                        else:
                            ok = False
                            break
                    if ok and entered:
                        takeoffs.append((tx, ty, climb, d, (px - tx) / d, (py - ty) / d))
            if takeoffs:
                out = {"H": H, "region": region, "takeoffs": takeoffs}
        self.plans[key] = out
        return out

    def _on_roof(self, x, y, z, H):
        """Feet at that roof's height and well above the walking height of our cell (a 4 m cell at a roof edge
        can be 'open' while our centre is still over the roof, so we test heights, not the blocked flag)."""
        c = self.g.cell_of(x, y)
        return c >= 0 and z >= H - 1.5 and (bool(self.g.blocked[c]) or z > self.g.surf[c] + 1.5)

    # ---------------------------------------------------------------- per tick
    def update(self, obs, me, now, attack_t, zone, walk, goal, t_out_fn, circle_fn, ground_d, other_banned):
        """None (rule off: caller runs sub-09) or {yaw, boost, jump, take, drop, phase}."""
        dt = 0.1 if self.prev_t is None else now - self.prev_t
        gap = self.prev_t is not None and dt > 0.25          # the caller skipped us (zone pressing)
        dt = min(max(dt, 0.0), 0.2)
        self.prev_t = now
        x, y, z = me["pos"]
        # ---- gates (also checked while committed): any failure = off at once
        if any(m.get("kind") == "mech" for m in obs.get("mechs") or []):
            return self._interrupt("mech in view")
        if now - attack_t < SAFE_HIT_S:
            return self._interrupt("recently hit")
        if not me.get("in_zone", True):
            return self._interrupt("outside zone")
        if gap and self.phase == "jet":
            self._interrupt("skipped")
        c = self.cur
        if c is not None and (c["pid"] in other_banned or c["pid"] in self.banned):
            self._done(x, y, z, now, "taken or failed take")
            c = None
        if c is None:
            if self.landed is not None:
                return self._backstop(x, y, z, now)
            if self.tries >= ROOF_MAX_TRIES or self.fails >= ROOF_MAX_FAILS:
                return None
            c = self._select(obs, me, now, zone, walk, goal, t_out_fn, circle_fn, ground_d, other_banned)
            if c is None:
                return None
            self.cur = c
            self.phase = "approach"
            self.log.append((now, f"commit {c['pid']} climb {c['climb']:.1f} cost {c['cost']:.0f}"))
        # ---- committed
        vis = next((p for p in obs.get("pickups") or [] if p.get("id") == c["pid"] and p.get("pos")), None)
        if vis is not None:
            c["px"], c["py"] = float(vis["pos"][0]), float(vis["pos"][1])
        c["spent"] += dt
        if c["spent"] > c["budget"]:
            return self._fail(x, y, z, now, "out of time", count=self.phase != "approach")
        if self.phase == "approach" and "jet_t0" in c and me.get("grounded", False) and z >= c["H"] - 0.8 \
                and self._on_roof(x, y, z, c["H"]):
            self.phase = "roof"                            # an interrupted jump that still landed us up there
            if self.landed is None:
                self.landed = {"t": now, "H": c["H"], "tx": c["tx"], "ty": c["ty"], "px": c["px"], "py": c["py"]}
        if self.phase == "approach":
            return self._approach(me, now, x, y, z, zone, walk, goal, t_out_fn, circle_fn, ground_d, vis)
        if self.phase == "jet":
            return self._jet(me, now, x, y, z, vis)
        return self._roof(me, now, x, y, z, vis)

    def _select(self, obs, me, now, zone, walk, goal, t_out_fn, circle_fn, ground_d, other_banned):
        g = self.g
        x, y = me["pos"][0], me["pos"][1]
        weapons = me.get("weapons") or []
        held = {w.get("kind") for w in weapons if w}
        free = weapons[1] if len(weapons) > 1 else None
        cur_rank = RANK.get(free.get("kind"), 1) if free else 0
        energy = float(me.get("energy", 0) or 0)
        drones = [m["pos"] for m in obs.get("mechs") or [] if m.get("kind") == "npc" and m.get("pos")]
        best = None
        for p in obs.get("pickups") or []:
            kind = p.get("kind")
            pid = p.get("id")
            if RANK.get(kind, 0) <= cur_rank or kind in held or pid in self.banned or pid in other_banned:
                continue
            if not p.get("pos"):
                continue
            px, py, pz = (float(v) for v in p["pos"])
            if math.hypot(px - x, py - y) > ROOF_M:
                continue
            cp = g.cell_of(px, py)
            if cp < 0 or not g.blocked[cp] or abs(g.top[cp] - pz) > ROOF_MATCH_DZ:
                continue                                   # not on a roof (the ground rule's business)
            pl = self.plan(pid, px, py, pz)
            if pl is None:
                continue
            if any(math.hypot(px - a[0], py - a[1]) < DRONE_M for a in drones):
                continue
            # nearest take-off point we can walk to in a straight line (checked nearest-first, at most 12)
            tos = sorted(pl["takeoffs"], key=lambda t: math.hypot(t[0] - x, t[1] - y))[:12]
            for tx, ty, climb, roof_d, ux, uy in tos:
                if energy < roof_energy_need(climb):
                    continue
                dT = math.hypot(tx - x, ty - y)
                if dT > ROOF_ARRIVE_M and not g.line_walkable(x, y, tx, ty):
                    continue
                if any(math.hypot(tx - a[0], ty - a[1]) < DRONE_M for a in drones):
                    continue
                if not self._zone_ok(now, zone, walk, goal, t_out_fn, circle_fn, px, py, tx, ty, dT, roof_d, climb):
                    continue
                cost = dT + roof_d + ROOF_CLIMB_COST * climb
                key = (-RANK[kind], cost)
                if best is None or key < best[0]:
                    best = (key, {"pid": pid, "kind": kind, "px": px, "py": py, "H": pl["H"], "tx": tx, "ty": ty,
                                  "ux": ux, "uy": uy, "climb": climb, "roof_d": roof_d, "cost": cost,
                                  "region": pl["region"], "need": roof_energy_need(climb), "spent": 0.0,
                                  "budget": 2.0 * (dT + roof_d) / max(walk, 1.0) + 8.0})
                break
        if best is None:
            return None
        if ground_d is not None and best[1]["cost"] >= ground_d:
            return None                                    # a ground pickup is closer: the ground rule wins
        return best[1]

    @staticmethod
    def _zone_ok(now, zone, walk, goal, t_out_fn, circle_fn, px, py, tx, ty, dT, roof_d, climb):
        gx, gy, gr = goal
        there = (dT + roof_d) / max(walk, 1.0) + climb / 8.0 + 1.0
        back = max(math.hypot(gx - px, gy - py) - gr, 0.0) * 1.5 / max(walk, 1.0)
        need = there + back + ZONE_SLACK_S + ROOF_EXTRA_ZONE_S
        if t_out_fn(zone, px, py, now) < need or t_out_fn(zone, tx, ty, now) < need:
            return False
        cx, cy, cr = circle_fn(zone, now + there + 5.0)
        return math.hypot(px - cx, py - cy) <= cr - 10.0 and math.hypot(tx - cx, ty - cy) <= cr - 10.0

    def _approach(self, me, now, x, y, z, zone, walk, goal, t_out_fn, circle_fn, ground_d, vis):
        c = self.cur
        dT = math.hypot(c["tx"] - x, c["ty"] - y)
        if not self._zone_ok(now, zone, walk, goal, t_out_fn, circle_fn, c["px"], c["py"], c["tx"], c["ty"],
                             dT, c["roof_d"], c["climb"]):
            return self._drop("zone")
        if math.hypot(c["px"] - x, c["py"] - y) > ROOF_M + 20.0:
            return self._drop("too far")
        if ground_d is not None and dT + c["roof_d"] + ROOF_CLIMB_COST * c["climb"] >= ground_d:
            return None                                    # a ground pickup is closer now: let it run, keep ours
        if dT > ROOF_ARRIVE_M:
            return {"yaw": math.atan2(c["ty"] - y, c["tx"] - x), "boost": False, "jump": False,
                    "take": None, "drop": None, "phase": "approach"}
        if float(me.get("energy", 0) or 0) < c["need"]:
            return {"yaw": None, "boost": False, "jump": False, "take": None, "drop": None, "phase": "approach"}
        if self.tries >= ROOF_MAX_TRIES or self.fails >= ROOF_MAX_FAILS:
            return self._drop("no tries left")
        self.tries += 1
        self.phase = "jet"
        c["jet_t0"] = now
        c["best_z"] = z
        c["best_t"] = now
        self.log.append((now, f"jump {c['pid']} try {self.tries}"))
        return self._jet(me, now, x, y, z, vis)

    def _jet(self, me, now, x, y, z, vis):
        c = self.cur
        H = c["H"]
        e = float(me.get("energy", 0) or 0)
        grounded = bool(me.get("grounded", False))
        if z > c["best_z"] + 0.3:
            c["best_z"] = z
            c["best_t"] = now
        if grounded and z >= H - 0.8:
            self.phase = "roof"
            self.landed = {"t": now, "H": H, "tx": c["tx"], "ty": c["ty"], "px": c["px"], "py": c["py"]}
            self.log.append((now, f"landed {c['pid']} energy {e:.0f}"))
            return self._roof(me, now, x, y, z, vis)
        if z < H - 0.3 and e < ROOF_MIN_ENERGY:
            return self._fail(x, y, z, now, "energy")
        if z < H + ROOF_CLEAR and now - c["best_t"] > ROOF_STALL_S:
            return self._fail(x, y, z, now, "no height gain")
        if now - c["jet_t0"] >= 0.3 and grounded and z < H - 2.0:
            return self._fail(x, y, z, now, "back on the ground")
        over = self.g.cell_of(x, y) in c["region"]
        if c["best_z"] < H + ROOF_CLEAR:
            yaw = math.atan2(c["uy"], c["ux"])          # along the checked line, into the wall, until clear
            boost = True
        else:
            # cleared once: coast over the roof; jet again only if we sink below roof + 1.5 m before we are over it
            yaw = math.atan2(c["py"] - y, c["px"] - x)
            boost = (not over) and z < H + ROOF_CLEAR
        return {"yaw": yaw, "boost": boost, "jump": grounded and z < H - 2.0, "take": None, "drop": None,
                "phase": "jet"}

    def _roof(self, me, now, x, y, z, vis):
        c = self.cur
        H = c["H"]
        if bool(me.get("grounded", False)) and z < H - 2.0:
            return self._fail(x, y, z, now, "fell off the roof")
        d = math.hypot(c["px"] - x, c["py"] - y)
        if vis is None and d < 8.0 and z >= H - 0.8:
            self.banned.add(c["pid"])
            self._done(x, y, z, now, "not there")
            return None
        weapons = me.get("weapons") or []
        free = weapons[1] if len(weapons) > 1 else None
        take_now = d <= self.reach - 0.5 and z >= H - 3.5
        return {"yaw": None if take_now else math.atan2(c["py"] - y, c["px"] - x), "boost": False, "jump": False,
                "take": c["pid"] if take_now else None, "drop": 1 if (take_now and free) else None,
                "phase": "roof"}

    def _up_high(self, x, y, z, L):
        """Still up on that roof, or on a lower part of the same block we stepped down to (near the pickup)."""
        c = self.g.cell_of(x, y)
        if c < 0 or math.hypot(L["px"] - x, L["py"] - y) > ROOF_REGION_M + 20.0:
            return False
        return z > self.g.surf[c] + 1.5 and z > self.g.ground[c] + 1.5

    def _backstop(self, x, y, z, now):
        L = self.landed
        if not self._up_high(x, y, z, L) or now - L["t"] > ROOF_BACKSTOP_END_S:
            self.landed = None                             # off the roof (or give up: the stuck rule is still on)
            self.phase = "off"
            return None
        if now - L["t"] < ROOF_BACKSTOP_S:
            return None
        # walk back along the line we came up (checked to be this roof all the way from the take-off edge to the
        # pickup): aim 5 m further along it from where we are, toward and then past the take-off point, until off
        ux, uy = L["tx"] - L["px"], L["ty"] - L["py"]
        n = math.hypot(ux, uy) or 1.0
        ux, uy = ux / n, uy / n
        s = (x - L["px"]) * ux + (y - L["py"]) * uy
        gx, gy = L["px"] + ux * (max(s, 0.0) + 5.0), L["py"] + uy * (max(s, 0.0) + 5.0)
        yaw = math.atan2(gy - y, gx - x)
        self.phase = "down"
        return {"yaw": yaw, "boost": False, "jump": False, "take": None, "drop": None, "phase": "down"}

    # ---------------------------------------------------------------- endings
    def _interrupt(self, why):
        """A gate failed: off at once. An attempt in the air is over (it was counted at the jump)."""
        self.why = why
        if self.phase == "jet":
            self.log.append((self.prev_t, f"interrupted in the air: {why}"))
            self.phase = "approach"
        elif self.phase == "down":
            self.phase = "off"
        return None

    def _done(self, x, y, z, now, why):
        self.why = why
        self.log.append((now, f"done: {why}"))
        self.cur = None
        self.phase = "off"

    def _drop(self, why):
        """Give up on the current pickup before jumping (not a failure; it can be chosen again later)."""
        self.why = why
        self.cur = None
        self.phase = "off"
        return None

    def _fail(self, x, y, z, now, why, count=True):
        c = self.cur
        self.why = why
        if c is not None:
            self.banned.add(c["pid"])
            if count:
                self.fails += 1
            self.log.append((now, f"fail {c['pid']}: {why} (fails {self.fails})"))
            if self.landed is None and "jet_t0" in c:
                # after any jump: arm the backstop (it only acts if we end up standing high near that roof)
                self.landed = {"t": now, "H": c["H"], "tx": c["tx"], "ty": c["ty"], "px": c["px"], "py": c["py"]}
        self.cur = None
        self.phase = "off"
        return None

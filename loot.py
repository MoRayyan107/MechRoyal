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
CUBE_M = 80.0             # walk to a visible cube this close (~7 s walk; sub-10, sub-09 used 60 m)
CRATE_M = 80.0            # shoot a visible ground crate this close (its cube must be fetchable too; sub-09: 60 m)
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

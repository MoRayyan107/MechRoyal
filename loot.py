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
        self.started = 0.0
        self.limit_s = 0.0
        self.banned = set()         # pickup ids we gave up on
        self.state = "off"

    def read_events(self, obs) -> None:
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
        self.read_events(obs)
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
        if free and free.get("kind") == "laser" and free.get("charge", 0):
            return self._off("laser charging")          # cannot swap out a charging laser
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
        if pid != self.target:
            self.target = pid
            self.started = now
            self.limit_s = 2.0 * d / max(walk, 1.0) + 4.0
        elif now - self.started > self.limit_s:
            self.banned.add(pid)
            self.target = None
            return self._off("gave up")
        drop = 1 if free else None
        yaw = math.atan2(py - y, px - x)
        take_now = d <= self.reach - 0.5 and abs(pz - z) < 3.5
        self.state = "take" if take_now else "go"
        return (yaw, pid, take_now, drop)

    def _off(self, why: str):
        self.state = "off"
        self.target = None
        return None

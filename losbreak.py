"""Break line of sight (sub-03): when an enemy mech can see us, get a building between us.

The rule, in plain words:

* ON while any enemy mech is listed in obs.mechs (the server keeps listing a mech for
  5 s after it leaves view), or a mech has hit us in the last HOLD_S seconds.
  OFF otherwise; then the normal (sub-02) movement runs unchanged.
* "Threats" are the nearest MAX_THREATS enemy mechs within THREAT_RANGE (beyond laser
  range nobody can hurt us). A mech that hit us but is not listed is placed UNSEEN_M
  away along the damage bearing.
* If our current spot is already hidden from every threat, stand still (facing the
  nearest threat). Otherwise walk, straight and at full speed (boost if energy allows),
  to the NEAREST cell within SEARCH_M that is hidden from every threat, reachable in a
  straight line, and inside the zone a few seconds from now. Nearest = the least time
  in the open.
* If no such cell exists within SEARCH_M, return None: the bot does its sub-02 thing.

"Hidden" means a BUILDING blocks the line from the threat's eye to the top of our mech
(trees do not count: they block sight but not shots). Blocking the top line also blocks
the line to our centre, because that line runs lower everywhere near us. To allow for
our 2.5 m radius, a candidate must also be hidden from 2 m to each side.

The search visits cells nearest-first and stops at the first hit; it is time-sliced
(it resumes next tick if it runs out of budget) and repeated at most every SCAN_EVERY_S.
"""
from __future__ import annotations

import math

import nav

SEARCH_M = 48.0       # ~4 s at walk speed (~2.5 s boosting): at most ~1-2 laser shots in the open
SCAN_EVERY_S = 0.5    # re-search this often while threats move
HOLD_S = 4.0          # stay in this mode this long after the last hit from a mech
THREAT_RANGE = 300.0  # laser range is 280 m, gun 230 m
MAX_THREATS = 3
NEAR_CHECK_M = 40.0   # only buildings within this distance of us count as our cover
UNSEEN_M = 120.0      # assumed distance of an attacker we cannot see
SIDE_M = 2.0          # half-width check for our body


class LosBreaker:
    def __init__(self, grid: "nav.Grid"):
        self.g = grid
        r = int(SEARCH_M / nav.CELL)
        offs = [(di * di + dj * dj, di, dj) for di in range(-r, r + 1) for dj in range(-r, r + 1)
                if 0 < di * di + dj * dj <= r * r]
        offs.sort()
        self.offs = [(di, dj) for _, di, dj in offs]   # nearest first
        self.target = None          # (x, y) hidden cell we are heading to
        self.scan_i = -1            # -1 = no scan in progress
        self.scan_from = None       # (ci, cj) cell the scan is centred on
        self.next_scan_t = -1.0
        self.state = "off"          # for logs/tests: off / hold / go / none / scanning

    # ---------------------------------------------------------------- geometry
    def _hidden_line(self, px, py, ptop, threats) -> bool:
        """True if, for every threat, a building within NEAR_CHECK_M of us blocks eye -> our top."""
        g = self.g
        blocked = g.blocked
        roof = g.top
        for ex, ey, ez in threats:
            dx = ex - px
            dy = ey - py
            d = math.hypot(dx, dy)
            if d < 3.0:
                return False
            ux = dx / d
            uy = dy / d
            steps = int(min(NEAR_CHECK_M, d - 2.0) / 2.0)
            hit = False
            for s in range(1, steps + 1):
                k = 2.0 * s
                c = g.cell_of(px + ux * k, py + uy * k)
                if c < 0:
                    break
                if blocked[c] and roof[c] > ptop + (ez - ptop) * (k / d):
                    hit = True
                    break
            if not hit:
                return False
        return True

    def hidden(self, px, py, ptop, threats) -> bool:
        """Hidden at our centre and SIDE_M to each side of every threat's line."""
        if not self._hidden_line(px, py, ptop, threats):
            return False
        for ex, ey, _ in threats:
            d = math.hypot(ex - px, ey - py) or 1.0
            sx = -(ey - py) / d * SIDE_M
            sy = (ex - px) / d * SIDE_M
            if not self._hidden_line(px + sx, py + sy, ptop, threats):
                return False
            if not self._hidden_line(px - sx, py - sy, ptop, threats):
                return False
        return True

    # ---------------------------------------------------------------- threats
    @staticmethod
    def threats(obs, x, y, z, attacker, attack_bearing, attack_t, now):
        out = []
        seen_attacker = False
        for m in obs.get("mechs") or []:
            if m.get("kind") != "mech" or not m.get("pos"):
                continue
            ex, ey, ez = m["pos"]
            d = math.hypot(ex - x, ey - y)
            if d > THREAT_RANGE:
                continue
            out.append((d, float(ex), float(ey), float(ez) + 8.0))
            if m.get("id") == attacker:
                seen_attacker = True
        recent_mech_hit = (attacker is not None and isinstance(attacker, int) and attacker < 100
                           and now - attack_t < HOLD_S)
        if recent_mech_hit and not seen_attacker:
            out.append((UNSEEN_M, x + UNSEEN_M * math.cos(attack_bearing),
                        y + UNSEEN_M * math.sin(attack_bearing), z + 8.0))
        out.sort()
        return [(ex, ey, ez) for _, ex, ey, ez in out[:MAX_THREATS]]

    # ---------------------------------------------------------------- per tick
    def update(self, obs, me, attacker, attack_bearing, attack_t, now, zone_circle, deadline, clock):
        """Return None (rule off / nothing found: use normal movement), ("hold", face_yaw),
        or ("go", move_yaw, face_yaw)."""
        x, y, z = me["pos"]
        threats = self.threats(obs, x, y, z, attacker, attack_bearing, attack_t, now)
        if not threats:
            self.state = "off"
            self.target = None
            self.scan_i = -1
            return None
        tx, ty, _ = threats[0]
        face = math.atan2(ty - y, tx - x)
        scale = 1.0 + 0.05 * float(me.get("cubes", 0) or 0)
        body = 9.0 * scale
        g = self.g

        # 1. already hidden where we stand: stay put
        if self.hidden(x, y, z + body, threats):
            self.state = "hold"
            self.target = (x, y)
            self.scan_i = -1
            return ("hold", face)

        # 2. keep heading for the current target while it still hides us
        if self.target is not None and now < self.next_scan_t:
            px, py = self.target
            c = g.cell_of(px, py)
            if c >= 0 and self.hidden(px, py, g.surf[c] + body, threats):
                self.state = "go"
                return ("go", math.atan2(py - y, px - x), face)

        # 3. (re)search nearest-first, time-sliced
        c0 = g.cell_of(x, y)
        if c0 < 0:
            return None
        if self.scan_i < 0:
            if self.target is None and now < self.next_scan_t and self.state == "none":
                return None                       # searched moments ago and found nothing
            self.scan_i = 0
            self.scan_from = (c0 % g.nx, c0 // g.nx)
            self.target = None
        ci, cj = self.scan_from
        zx, zy, zr = zone_circle
        zr2 = max(zr - 3.0, 0.0) ** 2
        offs = self.offs
        n = len(offs)
        i = self.scan_i
        while i < n:
            if clock() > deadline:
                self.scan_i = i
                self.state = "scanning"
                return None                       # resume next tick; normal movement meanwhile
            di, dj = offs[i]
            i += 1
            a = ci + di
            b = cj + dj
            if a < 1 or b < 1 or a >= g.nx - 1 or b >= g.ny - 1:
                continue
            c = b * g.nx + a
            if g.blocked[c]:
                continue
            px, py = g.center(c)
            if (px - zx) ** 2 + (py - zy) ** 2 > zr2:
                continue
            if not self._hidden_line(px, py, g.surf[c] + body, threats):
                continue
            if not g.line_walkable(x, y, px, py):
                continue
            if not self.hidden(px, py, g.surf[c] + body, threats):
                continue
            self.target = (px, py)
            self.scan_i = -1
            self.next_scan_t = now + SCAN_EVERY_S
            self.state = "go"
            return ("go", math.atan2(py - y, px - x), face)
        # nothing within SEARCH_M: fall back to normal movement, try again a little later
        self.scan_i = -1
        self.target = None
        self.next_scan_t = now + SCAN_EVERY_S
        self.state = "none"
        return None

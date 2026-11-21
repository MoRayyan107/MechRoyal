"""Survival-first mech bot for mechfight/1.

Scoring is placement (16 - place), so the bot's first job is to stay alive and its
second job is to be the one with the most HP when the zone closes. The behaviour is
built from small, readable rules, in priority order:

1. Zone: always walk (or boost when time is short) toward a point safely inside the
   circle the zone is shrinking to, following a precomputed walking-distance field
   so buildings never trap it. If it stops making progress it jumps and jets.
2. Hide: inside that circle it picks a hiding spot with the best cover (tree crowns
   and tall walls around it, away from drones), walks there quietly (under the 5 m/s
   footstep threshold) when time allows, and stands still. In the last circles
   (radius <= 25 m) it goes to the centre instead, which stays in the zone longest.
3. Hold fire: it only shoots back at whoever hit it in the last 4 s, or takes a
   nearly free kill. Not shooting keeps it silent and keeps HP regeneration on.
   When hit it turns to face the attacker (front hits do 1/1.5 of rear hits).
4. Break line of sight (sub-03, losbreak.py): while an enemy mech is in view or has
   hit us in the last 4 s, run to the nearest spot within 48 m where a building (not a
   tree) blocks its line to us, and stand there. The zone still wins when time is short;
   with no such spot in reach, rules 1-3 apply unchanged.
5. Upgrades: survival upgrades first (max HP, armour, regen, speed).

Every tick runs inside try/except; if the main logic fails, a tiny fallback (walk to
the zone centre) answers instead, so the mech never stands still because of a bug.
Logs go to stderr only.
"""
from __future__ import annotations

import gc
import math
import time
import traceback

import losbreak
import mechfight as mf
import nav

TWO_PI = 2.0 * math.pi
SLICE_S = 0.006          # time we allow the route search per tick (seconds)
LOS_SLICE_S = 0.005      # extra time we allow the line-of-sight cover search per tick
QUIET_SPEED = 4.5        # m/s; mechs only make footstep sounds above sound.step_speed (5 m/s)
QUIET_SLACK_S = 15.0     # walk quietly only if we would still beat the zone edge by this much
ENDGAME_R = 25.0         # when the next circle is this small, stand at its centre (cover is moot)
NPC_AVOID_M = 55.0       # keep hiding spots this far from drones we have seen (they aggro at 45 m)
SPOT_SEARCH_M = 120.0    # look for hiding spots within this distance (bounds the per-pick cost)

# Upgrade preference: survival first. Anything not listed is ranked after these.
UPGRADE_ORDER = ["plating", "armor", "nanites", "servos", "sensors", "capacitor", "jets",
                 "reactor", "gyros", "gun_rate", "gun_damage", "gun_velocity", "gun_mag"]


def wrap(a: float) -> float:
    """Angle to (-pi, pi]."""
    a = math.fmod(a + math.pi, TWO_PI)
    if a < 0:
        a += TWO_PI
    return a - math.pi


def log(msg: str) -> None:
    mf.log(f"[bot] {msg}")


class ZoneModel:
    """Answers: where will the zone edge be, and when does it reach a point?"""

    @staticmethod
    def circle_at(z: dict, t: float):
        cx, cy = z["center"]
        r = z["radius"]
        nx_, ny_ = z["next_center"]
        nr = z["next_radius"]
        ss = z["shrink_start"]
        se = z["shrink_end"]
        if t <= ss or se <= ss:
            return cx, cy, r
        f = min(1.0, (t - ss) / (se - ss))
        return cx + (nx_ - cx) * f, cy + (ny_ - cy) * f, r + (nr - r) * f

    @staticmethod
    def time_until_outside(z: dict, x: float, y: float, now: float) -> float:
        """Seconds until (x, y) falls outside the moving circle this phase (inf if never)."""
        nx_, ny_ = z["next_center"]
        if math.hypot(x - nx_, y - ny_) < z["next_radius"]:
            return math.inf   # inside the next circle = inside every circle this phase
        ss = z["shrink_start"]
        se = z["shrink_end"]
        cx, cy, r = ZoneModel.circle_at(z, now)
        if math.hypot(x - cx, y - cy) >= r:
            return 0.0
        t0 = max(now, ss)
        if se <= t0:
            return math.inf
        # sample the shrink window (the circle moves linearly, 40 samples is plenty)
        for k in range(1, 41):
            t = t0 + (se - t0) * k / 40.0
            cx, cy, r = ZoneModel.circle_at(z, t)
            if math.hypot(x - cx, y - cy) >= r:
                return t - now
        return math.inf


class Bot:
    name = "survivor"

    def __init__(self, hello: dict):
        self.cfg = hello.get("config", {}) or {}
        self.mech_cfg = self.cfg.get("mech", {}) or {}
        self.gun_cfg = (self.cfg.get("weapons", {}) or {}).get("gun", {}) or {}
        self.grid = None
        try:
            m = hello.get("map", {}) or {}
            self.grid = nav.Grid(m.get("dir", "/arena"), m.get("files", []), self.mech_cfg)
            log(f"grid {self.grid.nx}x{self.grid.ny} built in {self.grid.build_s:.2f}s")
        except Exception:
            log("map load failed, using straight-line moves:\n" + traceback.format_exc())
        self.losb = losbreak.LosBreaker(self.grid) if self.grid is not None else None
        self.los_errors = 0
        self.field = None
        self.field_key = None
        self.goal = (0.0, 0.0, 0.0)     # target circle x, y, radius
        self.spot = None                # (x, y) hiding spot inside the goal circle
        self.spot_t = -99.0             # when the spot was picked
        self.endgame = False            # next circle radius <= ENDGAME_R: stand at the centre
        self.npcs = {}                  # drone id -> last seen (x, y)
        # stuck detection
        self.hist = []                  # (t, x, y) of recent positions
        self.unstick_until = -1.0
        self.unstick_dir = 0
        # threat memory
        self.attacker = None            # id of whoever hit us last
        self.attack_bearing = 0.0
        self.attack_t = -99.0
        self.scan_yaw = 0.0
        self.slow_ticks = 0
        self.errors = 0
        gc.collect()
        gc.freeze()                     # the grid never changes: keep the GC from rescanning it

    # ------------------------------------------------------------------ tick entry
    def act(self, obs: dict) -> dict:
        t_start = time.perf_counter()
        try:
            act = self.decide(obs, t_start + SLICE_S, t_start + SLICE_S + LOS_SLICE_S)
        except Exception:
            self.errors += 1
            if self.errors <= 5 or self.errors % 200 == 0:
                log(f"decide failed at tick {obs.get('tick')} ({self.errors} so far):\n{traceback.format_exc()}")
            act = self.fallback(obs)
        dt = time.perf_counter() - t_start
        if dt > 0.02:
            self.slow_ticks += 1
            if self.slow_ticks <= 10:
                log(f"slow tick {obs.get('tick')}: {dt * 1000:.1f} ms")
        act["tick"] = obs["tick"]
        return act

    def fallback(self, obs: dict) -> dict:
        """Minimal safe action: head for the next zone centre, boost if outside the zone."""
        try:
            me = obs["self"]
            x, y = me["pos"][0], me["pos"][1]
            gx, gy = obs["zone"]["next_center"]
            yaw = math.atan2(gy - y, gx - x)
            far = math.hypot(gx - x, gy - y) > 5
            return {"move": [math.cos(yaw), math.sin(yaw)] if far else [0, 0], "aim": [yaw, 0.0],
                    "boost": not me.get("in_zone", True), "jump": not me.get("in_zone", True)}
        except Exception:
            return {}

    # ------------------------------------------------------------------ main logic
    def decide(self, obs: dict, deadline: float, los_deadline: float) -> dict:
        me = obs["self"]
        now = float(obs["t"])
        x, y, z = me["pos"]
        zone = obs["zone"]
        act: dict = {}

        self.pick_upgrade(obs, act)
        self.read_events(obs, now)

        # ---- 1. zone: choose the goal circle and keep its distance field up to date
        gx, gy = zone["next_center"]
        nr = float(zone["next_radius"])
        margin = min(20.0, 0.3 * nr)
        gr = max(nr - margin, 5.0)
        self.goal = (gx, gy, gr)
        self.endgame = nr <= ENDGAME_R
        self.remember_npcs(obs)
        key = (round(gx, 1), round(gy, 1), round(gr, 1))
        if self.grid is not None:
            repick = (self.spot is not None and now - self.spot_t > 10.0 and nr > ENDGAME_R
                      and self.npc_near(self.spot[0], self.spot[1]))
            if key != self.field_key or repick:
                # new circle (or a drone turned up next to our spot): pick a spot, start its field
                srcs = self.choose_spot(gx, gy, gr, nr, x, y, now)
                self.field = nav.Field(self.grid, srcs)
                self.field_key = key
        if self.field is not None and not self.field.done:
            self.field.step(deadline)

        move_yaw, dist_left = self.route(x, y, now)
        walk = float(me.get("stats", {}).get("walk_speed", self.mech_cfg.get("walk_speed", 11.7)))
        t_out = ZoneModel.time_until_outside(zone, x, y, now)
        boost = False
        under_fire = now - self.attack_t < 4.0
        if move_yaw is not None:
            eta = dist_left / max(walk, 1.0)
            boost = (not me.get("in_zone", True)) or eta + 4.0 > t_out
            # quiet walking: below the footstep threshold, when the zone gives us plenty of time
            quiet = QUIET_SPEED / max(walk, 1.0)
            slow_eta = dist_left / QUIET_SPEED
            speed = 1.0
            if not boost and not under_fire and slow_eta + QUIET_SLACK_S < t_out and quiet < 1.0:
                speed = quiet
            act["move"] = [math.cos(move_yaw) * speed, math.sin(move_yaw) * speed]
        else:
            act["move"] = [0.0, 0.0]

        # ---- break line of sight (sub-03): overrides the move only while a mech is in view or
        # just hit us, and never when the zone is pressing (boost above was set for the zone).
        face_yaw = None
        if self.losb is not None and not boost and me.get("in_zone", True):
            try:
                cx_, cy_, cr_ = ZoneModel.circle_at(zone, now + 5.0)
                r = self.losb.update(obs, me, self.attacker, self.attack_bearing, self.attack_t, now,
                                     (cx_, cy_, cr_), los_deadline, time.perf_counter)
            except Exception:
                r = None
                self.los_errors += 1
                if self.los_errors <= 5:
                    log(f"losbreak failed at tick {obs.get('tick')}: {traceback.format_exc()}")
            if r is not None:
                face_yaw = r[-1]
                if r[0] == "hold":
                    move_yaw = None
                    act["move"] = [0.0, 0.0]
                else:
                    move_yaw = r[1]
                    act["move"] = [math.cos(move_yaw), math.sin(move_yaw)]
                    boost = float(me.get("energy", 0)) > 30

        # ---- stuck: commanded to move but barely moved in the last second -> jump and jet
        jump = False
        if self.stuck(now, x, y, move_yaw is not None):
            self.unstick_until = now + 1.6
            # alternate a sidestep so we slide off whatever caught us
            self.unstick_dir = (self.unstick_dir + 1) % 3
            self.hist.clear()
        if now < self.unstick_until and move_yaw is not None:
            side = (0.0, 0.7, -0.7)[self.unstick_dir]
            yaw = move_yaw + side
            act["move"] = [math.cos(yaw), math.sin(yaw)]
            if me.get("grounded", False):
                jump = True
            boost = float(me.get("energy", 0)) > 8
        act["boost"] = bool(boost)
        act["jump"] = jump

        # ---- 2. aim: face/return fire on an attacker, otherwise look where we go or scan
        self.aim_and_fire(obs, act, now, move_yaw, face_yaw)
        return act

    # ------------------------------------------------------------------ helpers
    def route(self, x: float, y: float, now: float):
        """Return (yaw to walk, metres left) toward our spot, or (None, 0) once there.

        Without a finished field we walk straight to the goal circle and stop inside it."""
        gx, gy, gr = self.goal
        straight = math.hypot(gx - x, gy - y)
        g = self.grid
        f = self.field
        direct = math.atan2(gy - y, gx - x)
        if g is None or f is None or not f.done:
            if straight <= gr:
                return None, 0.0
            return direct, straight - gr
        c = g.cell_of(x, y)
        here = f.value(c)
        if self.endgame:
            # last circles: be within 8 m of the centre (the goal cells are those within 6 m, plus
            # half a cell). If the centre is inside a building the field only gets us to its foot;
            # then walk straight at the centre, and the stuck rule jets us onto the roof (on a roof
            # our cell is "blocked", so keep heading straight across it).
            if straight <= 8.0:
                return None, 0.0
            if here == 0.0 or (0 <= c < g.n and g.blocked[c]):
                return direct, straight
        if here == 0.0:
            return None, 0.0                     # standing on our spot: we have arrived
        # look at cells up to 3 away and walk toward the lowest one we can reach in a straight line
        best_c = -1
        best_v = here if here < nav.INF else nav.INF
        nxg = g.nx
        ci = c % nxg if c >= 0 else 0
        cj = c // nxg if c >= 0 else 0
        for dj in range(-3, 4):
            for di in range(-3, 4):
                if di == 0 and dj == 0:
                    continue
                i = ci + di
                j = cj + dj
                if i < 0 or j < 0 or i >= nxg or j >= g.ny:
                    continue
                k = j * nxg + i
                v = f.dist[k]
                # penalise far cells slightly less than their gain, so the longest clear step wins
                if v < best_v:
                    px, py = g.center(k)
                    if g.line_walkable(x, y, px, py) or here == nav.INF:
                        best_v = v
                        best_c = k
        if best_c < 0:
            if self.spot is not None:            # no better cell in reach: head straight (stuck logic jets)
                sx, sy = self.spot
                return math.atan2(sy - y, sx - x), math.hypot(sx - x, sy - y)
            return direct, max(straight - gr, 0.0)
        px, py = g.center(best_c)
        return math.atan2(py - y, px - x), best_v + math.hypot(px - x, py - y)

    def remember_npcs(self, obs: dict) -> None:
        """Drones stay within ~30 m of home, so where we last saw one is roughly where it lives."""
        for m in obs.get("mechs") or []:
            if m.get("kind") == "npc" and m.get("pos"):
                self.npcs[m.get("id")] = (float(m["pos"][0]), float(m["pos"][1]))

    def npc_near(self, x: float, y: float) -> bool:
        for nx_, ny_ in self.npcs.values():
            if math.hypot(nx_ - x, ny_ - y) < NPC_AVOID_M:
                return True
        return False

    def choose_spot(self, gx: float, gy: float, gr: float, nr: float, x: float, y: float, now: float):
        """Pick where to stand inside the goal circle; returns the field's source cells.

        Endgame (next radius <= 25 m): the circle centre. Otherwise the best-covered walkable cell
        within 80% of the goal radius and SPOT_SEARCH_M of us (or of the circle point nearest us),
        scored as cover minus 1 point per 40 m of distance, skipping cells near known drones."""
        g = self.grid
        self.spot_t = now
        if nr <= ENDGAME_R:
            self.spot = (gx, gy)
            return g.sources_in_circle(gx, gy, 6.0)
        inner = gr * 0.8
        d = math.hypot(x - gx, y - gy)
        if d <= inner:
            px, py = x, y
        else:
            px, py = gx + (x - gx) / d * inner, gy + (y - gy) / d * inner
        R = min(SPOT_SEARCH_M, inner + 1.0)
        best_c, best_s = -1, -1e9
        nxg = g.nx
        i0 = max(1, int((px - R - g.minx) / nav.CELL))
        i1 = min(nxg - 2, int((px + R - g.minx) / nav.CELL))
        j0 = max(1, int((py - R - g.miny) / nav.CELL))
        j1 = min(g.ny - 2, int((py + R - g.miny) / nav.CELL))
        inner2 = inner * inner
        R2 = R * R
        avoid2 = NPC_AVOID_M * NPC_AVOID_M
        # only drones that could be within NPC_AVOID_M of some cell in the search area matter
        reach = R + NPC_AVOID_M + nav.CELL
        npcs = [(a, b) for a, b in self.npcs.values() if (a - px) ** 2 + (b - py) ** 2 <= reach * reach]
        blocked = g.blocked
        cover = g.cover
        for j in range(j0, j1 + 1):
            cy = g.miny + (j + 0.5) * nav.CELL
            for i in range(i0, i1 + 1):
                c = j * nxg + i
                if blocked[c]:
                    continue
                cx = g.minx + (i + 0.5) * nav.CELL
                if (cx - gx) ** 2 + (cy - gy) ** 2 > inner2 or (cx - px) ** 2 + (cy - py) ** 2 > R2:
                    continue
                sc = cover[c] - math.hypot(cx - x, cy - y) / 40.0
                # same result as filtering drones first, but the drone test only runs for would-be winners
                if sc > best_s and not (npcs and any((cx - a) ** 2 + (cy - b) ** 2 < avoid2 for a, b in npcs)):
                    best_s, best_c = sc, c
        if best_c < 0:
            self.spot = None
            return g.sources_in_circle(gx, gy, gr)
        self.spot = g.center(best_c)
        out = [best_c]
        for off in (1, -1, nxg, -nxg):
            if not blocked[best_c + off]:
                out.append(best_c + off)
        return out

    def stuck(self, now: float, x: float, y: float, moving: bool) -> bool:
        if not moving:
            self.hist.clear()
            return False
        self.hist.append((now, x, y))
        while self.hist and now - self.hist[0][0] > 1.0:
            self.hist.pop(0)
        if len(self.hist) < 8 or now - self.hist[0][0] < 0.9:
            return False
        t0, x0, y0 = self.hist[0]
        return math.hypot(x - x0, y - y0) < 2.5 and now >= self.unstick_until

    def pick_upgrade(self, obs: dict, act: dict) -> None:
        offer = obs.get("offer")
        if not offer:
            return
        choices = offer.get("choices") or []
        best_i, best_rank = 0, 999
        for i, ch in enumerate(choices):
            cid = ch.get("id")
            rank = UPGRADE_ORDER.index(cid) if cid in UPGRADE_ORDER else 500 + i
            if rank < best_rank:
                best_i, best_rank = i, rank
        act["pick"] = best_i

    def read_events(self, obs: dict, now: float) -> None:
        for ev in obs.get("events") or []:
            if ev.get("type") == "damaged" and ev.get("by") is not None:
                self.attacker = ev.get("by")
                if ev.get("bearing") is not None:
                    self.attack_bearing = float(ev["bearing"])
                self.attack_t = now

    def aim_and_fire(self, obs: dict, act: dict, now: float, move_yaw, face_yaw=None) -> None:
        me = obs["self"]
        x, y, z = me["pos"]
        stats = me.get("stats", {}) or {}
        muzzle = (x, y, z + float(stats.get("muzzle_height", 7.0)))
        target = None
        recent = now - self.attack_t < 4.0
        for m in obs.get("mechs") or []:
            if recent and m.get("id") == self.attacker:
                target = m
                break
        if target is None:
            # a nearly free kill: a visible mech two bullets from death, close, with a clear line
            dmg = float((self.gun_cfg or {}).get("damage", 45.0))
            for m in obs.get("mechs") or []:
                if (m.get("kind") == "mech" and m.get("hp") is not None and float(m["hp"]) <= 2 * dmg
                        and math.dist(m["pos"][:2], (x, y)) < 100.0
                        and (self.grid is None or self.grid.los(
                            muzzle, (m["pos"][0], m["pos"][1], m["pos"][2] + 4.5)))):
                    target = m
                    break

        gun = None
        for w in me.get("weapons") or []:
            if w and w.get("kind") == "gun":
                gun = w
        if target is not None and gun is not None:
            gs = gun.get("stats", {}) or self.gun_cfg
            speed = float(gs.get("speed", 252.0))
            rng = float(gs.get("range", 230.0))
            tx, ty, tz = target["pos"]
            vx, vy, vz = (target.get("vel") or [0, 0, 0])
            aim_z = tz + (4.5 if target.get("kind") == "mech" else 1.75)
            # lead the target: two fixed-point iterations of flight time
            t_f = 0.0
            for _ in range(2):
                px, py, pz = tx + vx * t_f, ty + vy * t_f, aim_z + vz * t_f
                t_f = math.dist((px, py, pz), muzzle) / speed
            px, py, pz = tx + vx * t_f, ty + vy * t_f, aim_z + vz * t_f
            horiz = math.hypot(px - muzzle[0], py - muzzle[1])
            yaw = math.atan2(py - muzzle[1], px - muzzle[0])
            pitch = math.atan2(pz - muzzle[2], max(horiz, 0.1))
            act["aim"] = [yaw, pitch]
            cur = me.get("aim") or [yaw, pitch]
            tol = max(0.015, 2.0 / max(horiz, 1.0))
            clear = self.grid is None or self.grid.los(muzzle, (px, py, pz))
            if (horiz < rng and clear and abs(wrap(cur[0] - yaw)) < tol
                    and abs(cur[1] - pitch) < tol * 1.5):
                act["fire"] = [True, False]
            return

        if recent:
            # hit by something we can't see: face it, front armour takes the hits
            act["aim"] = [self.attack_bearing, 0.0]
        elif face_yaw is not None:
            act["aim"] = [face_yaw, 0.0]           # breaking line of sight: keep the front armour on them
        elif move_yaw is not None:
            act["aim"] = [move_yaw, 0.0]
        else:
            # holding position: sweep the view slowly so threats show up early
            self.scan_yaw = wrap(self.scan_yaw + 0.12)
            act["aim"] = [self.scan_yaw, 0.0]
        if gun is not None and gun.get("ammo", 1) < float((gun.get("stats") or {}).get("mag", 12)) / 3 \
                and gun.get("reload", 0) == 0:
            act["reload"] = True


if __name__ == "__main__":
    mf.run(Bot)

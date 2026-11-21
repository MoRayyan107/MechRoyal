"""Survival-first mech bot for mechfight/1.

Scoring is placement (16 - place), so the bot's first job is to stay alive and its
second job is to be the one with the most HP when the zone closes. The behaviour is
built from small, readable rules, in priority order:

1. Zone: always walk (or boost when time is short) toward a point safely inside the
   circle the zone is shrinking to, following a precomputed walking-distance field
   so buildings never trap it. If it stops making progress it jumps and jets.
2. Defence: when something damages it, it turns its torso to face the attacker
   (front hits do 1/1.5 of rear hits) and returns gun fire if it can see it.
3. Upgrades: survival upgrades first (max HP, armour, regen, speed).
4. Weapons (sub-04, loot.py): when no mech is in view, none hit us in the last 4 s and
   the zone is not pressing, walk to a visible laser (or lightning) within 80 m and take
   it. Fire the weapon that works at the range: lightning within its 45 m arc range, the
   laser (charge, then release on target) out to its 280 m range, the gun as before.
5. Cubes (sub-09, loot.CubeHunter): with the same safety gates, walk onto a visible cube
   within 80 m (weapons first), and shoot a visible ground crate within 80 m with the gun
   while walking toward it (its cube drops there; >= 9 rounds to start, at most 4 crates a
   match), reloading early after.
   Each cube is +4% HP and damage, and the 1st, 3rd, 6th ... bring an upgrade (plating first).

Every tick runs inside try/except; if the main logic fails, a tiny fallback (walk to
the zone centre) answers instead, so the mech never stands still because of a bug.
Logs go to stderr only.
"""
from __future__ import annotations

import gc
import math
import time
import traceback

import loot
import mechfight as mf
import nav

TWO_PI = 2.0 * math.pi
SLICE_S = 0.006          # time we allow the route search per tick (seconds)
CRATE_STOP_M = 8.0       # sub-09: stop walking toward a crate we are shooting this close to it

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
        self.field = None
        self.field_key = None
        self.goal = (0.0, 0.0, 0.0)     # target circle x, y, radius
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
        reach = float((self.cfg.get("loot", {}) or {}).get("pickup_radius", 4.0))
        self.looter = loot.Looter(self.grid, reach) if self.grid is not None else None
        self.loot_errors = 0
        self.cuber = loot.CubeHunter(self.grid) if self.grid is not None else None   # sub-09
        self.cube_errors = 0
        self.crate_pick = None          # sub-09: (aim point, crate id) chosen this tick, or None
        gc.collect()
        gc.freeze()                     # the grid never changes: keep the GC from rescanning it

    # ------------------------------------------------------------------ tick entry
    def act(self, obs: dict) -> dict:
        t_start = time.perf_counter()
        try:
            act = self.decide(obs, t_start + SLICE_S)
        except Exception:
            self.errors += 1
            if self.errors <= 5 or self.errors % 200 == 0:
                log(f"decide failed at tick {obs.get('tick')} ({self.errors} so far):\n{traceback.format_exc()}")
            act = self.fallback(obs)
            try:
                self.pick_upgrade(obs, act)      # sub-09: never lose an upgrade choice to a crash
            except Exception:
                pass
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
    def decide(self, obs: dict, deadline: float) -> dict:
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
        key = (round(gx, 1), round(gy, 1), round(gr, 1))
        if self.grid is not None and key != self.field_key:
            self.field = nav.Field(self.grid, self.grid.sources_in_circle(gx, gy, gr))
            self.field_key = key
        if self.field is not None and not self.field.done:
            self.field.step(deadline)

        move_yaw, dist_left = self.route(x, y, now)
        walk = float(me.get("stats", {}).get("walk_speed", self.mech_cfg.get("walk_speed", 11.7)))
        t_out = ZoneModel.time_until_outside(zone, x, y, now)
        boost = False
        if move_yaw is not None:
            eta = dist_left / max(walk, 1.0)
            boost = (not me.get("in_zone", True)) or eta + 4.0 > t_out
            act["move"] = [math.cos(move_yaw), math.sin(move_yaw)]
        else:
            act["move"] = [0.0, 0.0]

        # ---- weapons (sub-04): fetch a better weapon when it is close and safe. The zone wins:
        # the rule is skipped whenever the zone boost condition above is on or we are outside.
        # Take results are read on every tick, even when the rule is skipped (sub-05b fix #6).
        if self.looter is not None:
            try:
                self.looter.read_events(obs)
            except Exception:
                self.loot_errors += 1
                if self.loot_errors <= 5:
                    log(f"loot events failed at tick {obs.get('tick')}: {traceback.format_exc()}")
        r = None
        if self.looter is not None and not boost and me.get("in_zone", True):
            try:
                r = self.looter.update(obs, me, now, self.attacker, self.attack_t, zone, walk, self.goal,
                                       ZoneModel.time_until_outside, ZoneModel.circle_at)
            except Exception:
                r = None
                self.loot_errors += 1
                if self.loot_errors <= 5:
                    log(f"loot failed at tick {obs.get('tick')}: {traceback.format_exc()}")
            if r is not None:
                yaw, pid, take_now, drop = r
                if take_now:
                    act["take"] = pid
                    if drop is not None:
                        act["drop"] = drop
                    move_yaw = None
                    act["move"] = [0.0, 0.0]
                else:
                    move_yaw = yaw
                    act["move"] = [math.cos(yaw), math.sin(yaw)]

        # ---- cubes (sub-09): walk onto a close, safe cube. Weapons first: only when the weapon rule did nothing.
        zone_ok = bool(me.get("in_zone", True)) and not boost
        cube_yaw = None
        if self.cuber is not None and r is None and zone_ok:
            try:
                cube_yaw = self.cuber.update_cube(obs, me, now, self.attacker, self.attack_t, zone, walk, self.goal,
                                                  ZoneModel.time_until_outside, ZoneModel.circle_at)
            except Exception:
                cube_yaw = None
                self.cube_errors += 1
                if self.cube_errors <= 5:
                    log(f"cube failed at tick {obs.get('tick')}: {traceback.format_exc()}")
            if cube_yaw is not None:
                move_yaw = cube_yaw
                act["move"] = [math.cos(cube_yaw), math.sin(cube_yaw)]
        # ---- crates (sub-09): no weapon or cube to fetch -> shoot a close, safe ground crate, walking toward it
        # (its cube drops where it stands; walking the route instead would carry us out of range of it).
        self.crate_pick = None
        if self.cuber is not None and r is None and cube_yaw is None and zone_ok and now - self.attack_t >= 4.0:
            gun = next((w for w in (me.get("weapons") or [])[:2] if w and w.get("kind") == "gun"), None)
            muzzle = (x, y, z + float((me.get("stats") or {}).get("muzzle_height", 7.0)))
            try:
                self.crate_pick = self.cuber.pick_crate(obs, me, now, self.attacker, self.attack_t, zone_ok, zone,
                                                        walk, self.goal, ZoneModel.time_until_outside,
                                                        ZoneModel.circle_at, gun, muzzle)
            except Exception:
                self.crate_pick = None
                self.cube_errors += 1
                if self.cube_errors <= 5:
                    log(f"crate failed at tick {obs.get('tick')}: {traceback.format_exc()}")
            if self.crate_pick is not None:
                (cx, cy, _), _ = self.crate_pick
                if math.hypot(cx - x, cy - y) > CRATE_STOP_M:
                    move_yaw = math.atan2(cy - y, cx - x)
                    act["move"] = [math.cos(move_yaw), math.sin(move_yaw)]
                else:
                    move_yaw = None
                    act["move"] = [0.0, 0.0]

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
        self.aim_and_fire(obs, act, now, move_yaw, zone_ok)
        return act

    # ------------------------------------------------------------------ helpers
    def route(self, x: float, y: float, now: float):
        """Return (yaw to walk, metres left) toward the goal circle, or (None, 0) if inside it."""
        gx, gy, gr = self.goal
        straight = math.hypot(gx - x, gy - y)
        if straight <= gr:
            return None, 0.0
        g = self.grid
        f = self.field
        direct = math.atan2(gy - y, gx - x)
        if g is None or f is None or not f.done:
            return direct, straight - gr
        c = g.cell_of(x, y)
        here = f.value(c)
        if here == 0.0:
            return None, 0.0                     # standing in a goal cell: we have arrived
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
            return direct, straight - gr         # no better cell in reach: head straight (stuck logic jets)
        px, py = g.center(best_c)
        return math.atan2(py - y, px - x), best_v + math.hypot(px - x, py - y)

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

    def gun_solution(self, gun, target, muzzle):
        """Lead the target for the gun: returns (yaw, pitch, horizontal distance, range)."""
        gs = (gun or {}).get("stats", {}) or self.gun_cfg
        speed = float(gs.get("speed", 252.0))
        rng = float(gs.get("range", 230.0))
        tx, ty, tz = target["pos"]
        vx, vy, vz = (target.get("vel") or [0, 0, 0])
        aim_z = tz + (4.5 if target.get("kind") == "mech" else 1.75)
        t_f = 0.0
        for _ in range(2):
            px, py, pz = tx + vx * t_f, ty + vy * t_f, aim_z + vz * t_f
            t_f = math.dist((px, py, pz), muzzle) / speed
        px, py, pz = tx + vx * t_f, ty + vy * t_f, aim_z + vz * t_f
        horiz = math.hypot(px - muzzle[0], py - muzzle[1])
        return (math.atan2(py - muzzle[1], px - muzzle[0]), math.atan2(pz - muzzle[2], max(horiz, 0.1)),
                horiz, rng, (px, py, pz))

    def fire_special(self, me, act, target, muzzle, weapons, gun, gun_i, laser_i, bolt_i) -> bool:
        """Use lightning or the laser when the range suits it. Returns False to let the gun code run.

        * lightning: target within 90% of its range, aim straight at it (it is instant), fire when the
          torso is within a quarter of the cone and a building does not block the line; keep 10 energy
          spare for jumping out of trouble.
        * laser: target within 95% of its range and the laser ready (or already charging): aim straight
          at the target (hitscan, no lead), hold the trigger to charge, release when fully charged and
          on target. A clear line is required to start a charge.
        * the gun also fires in the same tick if its own lead solution happens to be on target.
        """
        wcfg = self.cfg.get("weapons", {}) or {}
        tx, ty, tz = target["pos"]
        aim_pt = (tx, ty, tz + (4.5 if target.get("kind") == "mech" else 1.75))
        horiz = math.hypot(aim_pt[0] - muzzle[0], aim_pt[1] - muzzle[1])
        yaw = math.atan2(aim_pt[1] - muzzle[1], aim_pt[0] - muzzle[0])
        pitch = math.atan2(aim_pt[2] - muzzle[2], max(horiz, 0.1))
        cur = me.get("aim") or [yaw, pitch]
        err_y = abs(wrap(cur[0] - yaw))
        err_p = abs(cur[1] - pitch)
        clear = self.grid is None or self.grid.los(muzzle, aim_pt)
        fire = [False, False]
        mode = None
        if bolt_i is not None:
            w = weapons[bolt_i]
            st = w.get("stats") or wcfg.get("lightning", {}) or {}
            rng = float(st.get("range", 45.0))
            cone = math.radians(float(st.get("cone_deg", 30.0)))
            cost = float(st.get("energy", 3.0))
            if horiz <= rng * 0.9 and float(me.get("energy", 0)) >= cost + 10.0:
                mode = "lightning"
                if clear and err_y < cone / 4 and float(w.get("cooldown", 0)) <= 0.0:
                    fire[bolt_i] = True
        if mode is None and laser_i is not None:
            w = weapons[laser_i]
            st = w.get("stats") or wcfg.get("laser", {}) or {}
            rng = float(st.get("range", 280.0))
            full = float(st.get("charge", 0.75))
            charge = float(w.get("charge", 0) or 0)
            ready = float(w.get("cooldown", 0) or 0) <= 0.2
            if horiz <= rng * 0.95 and (ready or charge > 0):
                mode = "laser"
                tol = max(0.01, 1.5 / max(horiz, 1.0))
                on = err_y < tol and err_p < tol * 1.5
                if charge > 0:
                    # keep charging until full; then release (fire=False) once on target
                    fire[laser_i] = not (charge >= full - 0.05 and on and clear)
                elif clear and err_y < 0.3:
                    fire[laser_i] = True          # start charging while the torso finishes turning
        if mode is None:
            return False
        act["aim"] = [yaw, pitch]
        if gun is not None and gun_i is not None:
            gyaw, gpitch, ghoriz, grng, gpt = self.gun_solution(gun, target, muzzle)
            gtol = max(0.015, 2.0 / max(ghoriz, 1.0))
            if ghoriz < grng and clear and abs(wrap(cur[0] - gyaw)) < gtol and abs(cur[1] - gpitch) < gtol * 1.5:
                fire[gun_i] = True
        act["fire"] = fire
        return True

    def aim_and_fire(self, obs: dict, act: dict, now: float, move_yaw, zone_ok: bool = False) -> None:
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
            # anyone mech-kind very close is a threat whether or not it has hit us yet
            close = [m for m in obs.get("mechs") or [] if m.get("kind") == "mech"
                     and math.dist(m["pos"][:2], (x, y)) < 45.0]
            if close:
                target = min(close, key=lambda m: math.dist(m["pos"][:2], (x, y)))

        gun = None
        gun_i = laser_i = bolt_i = None
        weapons = me.get("weapons") or []
        for i, w in enumerate(weapons[:2]):
            if not w:
                continue
            if w.get("kind") == "gun":
                gun, gun_i = w, i
            elif w.get("kind") == "laser":
                laser_i = i
            elif w.get("kind") == "lightning":
                bolt_i = i
        if target is not None and (laser_i is not None or bolt_i is not None):
            if self.fire_special(me, act, target, muzzle, weapons, gun, gun_i, laser_i, bolt_i):
                return
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
                fire = [False, False]
                fire[gun_i if gun_i is not None else 0] = True
                act["fire"] = fire
            return

        # sub-09: nobody to fight and decide() chose a crate -> aim the gun at it (the legs walk toward it)
        pick = self.crate_pick
        if not recent and gun is not None and gun_i is not None and self.cuber is not None:
            if pick is not None:
                (cx, cy, cz), kid = pick
                horiz = math.hypot(cx - muzzle[0], cy - muzzle[1])
                yaw = math.atan2(cy - muzzle[1], cx - muzzle[0])
                pitch = math.atan2(cz - muzzle[2], max(horiz, 0.1))
                act["aim"] = [yaw, pitch]
                cur = me.get("aim") or [yaw, pitch]
                tol = max(0.01, 1.0 / max(horiz, 1.0))          # crate radius 1.4 m, half height 1.2 m
                ammo = int(gun.get("ammo", 0) or 0)
                reloading = float(gun.get("reload", 0) or 0) > 0
                on = abs(wrap(cur[0] - yaw)) < tol and abs(cur[1] - pitch) < tol
                if ammo > 0 and not reloading and on:
                    fire = [False, False]
                    fire[gun_i] = True
                    act["fire"] = fire
                    self.cuber.crates_started.add(kid)
                    self.cuber.last_shot_t = now
                elif ammo == 0 and not reloading:
                    act["reload"] = True
                return

        if recent:
            # hit by something we can't see: face it, front armour takes the hits
            act["aim"] = [self.attack_bearing, 0.0]
        elif move_yaw is not None:
            act["aim"] = [move_yaw, 0.0]
        else:
            # holding position: sweep the view slowly so threats show up early
            self.scan_yaw = wrap(self.scan_yaw + 0.12)
            act["aim"] = [self.scan_yaw, 0.0]
        if gun is not None and gun.get("ammo", 1) < float((gun.get("stats") or {}).get("mag", 12)) / 3 \
                and gun.get("reload", 0) == 0:
            act["reload"] = True
        elif (gun is not None and self.cuber is not None and now - self.cuber.last_shot_t < 5.0
              and gun.get("ammo", 99) < loot.CRATE_RELOAD_BELOW and gun.get("reload", 0) == 0):
            act["reload"] = True                    # sub-09: reload early after shooting a crate


if __name__ == "__main__":
    mf.run(Bot)

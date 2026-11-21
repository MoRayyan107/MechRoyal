"""Map model and route planning for the mech bot (standard library only).

What this file does, in plain words:

* At start-up it turns the static map (/arena/v2/world.json + terrain.i16) into a
  coarse grid of 4 m cells. Every cell gets a walking height, and a flag saying
  whether a building stands on it ("blocked"), or whether a building is within a
  mech's radius of it ("near": walkable, but routes avoid hugging walls).
* For a goal region (cells inside the circle the zone is shrinking to), it builds a
  "distance field": for every cell, the walking distance to the goal. Following the
  field downhill always leads to the goal around buildings, never into a dead end.
  The field is computed a little at a time (a few milliseconds per tick) so the bot
  never misses its 50 ms answer budget.
* It also answers cheap line-of-sight questions (does a building stand between two
  points?) using each blocked cell's roof height.

Nothing here allocates much per tick; the heavy work is the one-off grid build and
the time-sliced field.
"""
from __future__ import annotations

import heapq
import json
import math
import os
import sys
import time
from array import array

INF = float("inf")
CELL = 4.0          # grid cell size in metres
HEAD_CLEAR = 9.5    # a solid whose underside is this far above the ground can be walked under
NEAR_COST = 2.5     # cost multiplier for cells that hug a wall
RISE_ORTH = 2.6     # max climb (m) between two side-by-side cells that we route over
RISE_DIAG = 3.6     # same, diagonal neighbours


def _point_in_ring(x: float, y: float, ring) -> bool:
    """Even-odd ray cast against a flat [x0, y0, x1, y1, ...] polygon."""
    inside = False
    n = len(ring) // 2
    j = n - 1
    for i in range(n):
        xi = ring[2 * i]
        yi = ring[2 * i + 1]
        xj = ring[2 * j]
        yj = ring[2 * j + 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


class Grid:
    """Static walkability grid built once from the map files."""

    def __init__(self, map_dir: str, files, mech_cfg: dict):
        t0 = time.perf_counter()
        world_path = None
        terrain_path = None
        for f in files or []:
            if f.endswith("world.json"):
                world_path = os.path.join(map_dir, f)
            elif f.endswith(".i16"):
                terrain_path = os.path.join(map_dir, f)
        world_path = world_path or os.path.join(map_dir, "v2", "world.json")
        terrain_path = terrain_path or os.path.join(map_dir, "v2", "terrain.i16")
        with open(world_path, "r", encoding="utf-8") as fh:
            world = json.load(fh)

        # --- terrain heights (1 m vertex grid, int16 centimetres) ---
        t = world["terrain"]
        self.t_nx = int(t["nx"])
        self.t_ny = int(t["ny"])
        self.t_minx = float(t["minX"])
        self.t_miny = float(t["minY"])
        self.t_step = float(t.get("step", 1.0))
        self.t_scale = float(t.get("scale", 0.01))
        terr = array("h")
        with open(terrain_path, "rb") as fh:
            terr.frombytes(fh.read())
        if sys.byteorder != "little":
            terr.byteswap()
        self.terr = terr

        # --- coarse grid, with a one-cell blocked border so neighbour loops need no bounds checks ---
        b = world["bounds"]
        self.minx = float(b["minX"]) - CELL
        self.miny = float(b["minY"]) - CELL
        self.nx = int(math.ceil((float(b["maxX"]) - float(b["minX"])) / CELL)) + 2
        self.ny = int(math.ceil((float(b["maxY"]) - float(b["minY"])) / CELL)) + 2
        nx, ny = self.nx, self.ny
        n = nx * ny
        self.n = n
        step_up = float(mech_cfg.get("step_up", 1.2))
        radius = float(mech_cfg.get("radius", 2.5))

        ground = array("d", [0.0]) * n
        for j in range(ny):
            cy = self.miny + (j + 0.5) * CELL
            for i in range(nx):
                ground[j * nx + i] = self.terrain_h(self.minx + (i + 0.5) * CELL, cy)
        self.ground = ground
        self.surf = array("d", ground)            # walking height (ground, or a low deck/platform)
        self.top = array("d", [-INF]) * n          # roof height of a solid on this cell (for line of sight)
        self.blocked = bytearray(n)                # 1 = a building stands here
        self.near = bytearray(n)                   # 1 = a building is within a mech radius
        for i in range(nx):
            self.blocked[i] = 1
            self.blocked[(ny - 1) * nx + i] = 1
        for j in range(ny):
            self.blocked[j * nx] = 1
            self.blocked[j * nx + nx - 1] = 1

        # --- rasterise every prism (buildings, stadium parts, bridges) ---
        offs = ((radius, 0.0), (-radius, 0.0), (0.0, radius), (0.0, -radius))
        for p in world.get("prisms", []):
            ring = p.get("ring") or []
            if len(ring) < 6:
                continue
            holes = [h for h in (p.get("holes") or []) if len(h) >= 6]
            base = float(p.get("base", 0.0))
            top = float(p.get("top", 0.0))
            is_bridge = p.get("kind") == "bridge"
            xs = ring[0::2]
            ys = ring[1::2]
            i0 = max(1, int((min(xs) - radius - self.minx) / CELL))
            i1 = min(nx - 2, int((max(xs) + radius - self.minx) / CELL))
            j0 = max(1, int((min(ys) - radius - self.miny) / CELL))
            j1 = min(ny - 2, int((max(ys) + radius - self.miny) / CELL))

            def inside(x, y):
                if not _point_in_ring(x, y, ring):
                    return False
                for h in holes:
                    if _point_in_ring(x, y, h):
                        return False
                return True

            for j in range(j0, j1 + 1):
                cy = self.miny + (j + 0.5) * CELL
                for i in range(i0, i1 + 1):
                    c = j * nx + i
                    g = ground[c]
                    if base >= g + HEAD_CLEAR:
                        continue                      # high bridge / overhang: walk under it
                    walk_on = top <= g + step_up or is_bridge
                    cx = self.minx + (i + 0.5) * CELL
                    if inside(cx, cy):
                        if walk_on:
                            if top > self.surf[c]:
                                self.surf[c] = top    # a low deck or a bridge: walk on top
                        else:
                            self.blocked[c] = 1
                            if top > self.top[c]:
                                self.top[c] = top
                    elif not walk_on:
                        for ox, oy in offs:
                            if inside(cx + ox, cy + oy):
                                self.near[c] = 1
                                break

        self.free_count = n - sum(self.blocked)
        self.build_s = time.perf_counter() - t0

    # ---------- lookups ----------
    def terrain_h(self, x: float, y: float) -> float:
        """Bilinear ground height at (x, y)."""
        fx = (x - self.t_minx) / self.t_step
        fy = (y - self.t_miny) / self.t_step
        i = int(math.floor(fx))
        j = int(math.floor(fy))
        if i < 0:
            i, fx = 0, 0.0
        if j < 0:
            j, fy = 0, 0.0
        if i >= self.t_nx - 1:
            i, fx = self.t_nx - 2, float(self.t_nx - 1)
        if j >= self.t_ny - 1:
            j, fy = self.t_ny - 2, float(self.t_ny - 1)
        ax = fx - i
        ay = fy - j
        nx = self.t_nx
        t = self.terr
        h00 = t[j * nx + i]
        h10 = t[j * nx + i + 1]
        h01 = t[(j + 1) * nx + i]
        h11 = t[(j + 1) * nx + i + 1]
        h0 = h00 + (h10 - h00) * ax
        h1 = h01 + (h11 - h01) * ax
        return (h0 + (h1 - h0) * ay) * self.t_scale

    def cell_of(self, x: float, y: float) -> int:
        i = int((x - self.minx) / CELL)
        j = int((y - self.miny) / CELL)
        if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
            return -1
        return j * self.nx + i

    def center(self, c: int):
        return (self.minx + (c % self.nx + 0.5) * CELL, self.miny + (c // self.nx + 0.5) * CELL)

    def walkable(self, c: int) -> bool:
        return 0 <= c < self.n and not self.blocked[c]

    def line_walkable(self, x0, y0, x1, y1) -> bool:
        """True if the straight walk from (x0,y0) to (x1,y1) crosses no blocked cell (2 m samples)."""
        d = math.hypot(x1 - x0, y1 - y0)
        k = max(1, int(d / 2.0))
        for s in range(1, k + 1):
            f = s / k
            c = self.cell_of(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)
            if c < 0 or self.blocked[c]:
                return False
        return True

    def los(self, a, b) -> bool:
        """Line of sight between 3-D points a and b: False if a building roof is above the line."""
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        d = math.hypot(dx, dy)
        k = max(1, int(d / 2.0))
        for s in range(1, k):
            f = s / k
            c = self.cell_of(a[0] + dx * f, a[1] + dy * f)
            if c < 0:
                return False
            if self.blocked[c] and a[2] + (b[2] - a[2]) * f < self.top[c]:
                return False
        return True

    def sources_in_circle(self, cx: float, cy: float, r: float):
        """Walkable cells whose centre lies within r of (cx, cy); falls back to the nearest ones."""
        out = []
        i0 = max(1, int((cx - r - self.minx) / CELL))
        i1 = min(self.nx - 2, int((cx + r - self.minx) / CELL))
        j0 = max(1, int((cy - r - self.miny) / CELL))
        j1 = min(self.ny - 2, int((cy + r - self.miny) / CELL))
        r2 = r * r
        for j in range(j0, j1 + 1):
            y = self.miny + (j + 0.5) * CELL - cy
            for i in range(i0, i1 + 1):
                c = j * self.nx + i
                if self.blocked[c]:
                    continue
                x = self.minx + (i + 0.5) * CELL - cx
                if x * x + y * y <= r2:
                    out.append(c)
        if not out:
            # goal centre is inside a building: take the closest walkable cells (search outward)
            for rr in (12.0, 24.0, 48.0, 96.0):
                if rr > r:
                    out = self.sources_in_circle(cx, cy, rr)
                    if out:
                        break
        return out


class Field:
    """Walking distance from every cell to a goal region, built a slice at a time (Dijkstra)."""

    def __init__(self, grid: Grid, sources):
        self.g = grid
        self.dist = [INF] * grid.n
        self.heap = []
        for c in sources:
            self.dist[c] = 0.0
            self.heap.append((0.0, c))
        heapq.heapify(self.heap)
        self.done = not self.heap
        self.pops = 0
        nx = grid.nx
        # (offset, step length in cells, max climb, side cell a, side cell b); sides are 0 for straight moves.
        # A diagonal step is only allowed when both cells it squeezes between are free (no corner cutting).
        self.nbrs = ((1, 1.0, RISE_ORTH, 0, 0), (-1, 1.0, RISE_ORTH, 0, 0),
                     (nx, 1.0, RISE_ORTH, 0, 0), (-nx, 1.0, RISE_ORTH, 0, 0),
                     (nx + 1, 1.4142, RISE_DIAG, 1, nx), (nx - 1, 1.4142, RISE_DIAG, -1, nx),
                     (-nx + 1, 1.4142, RISE_DIAG, 1, -nx), (-nx - 1, 1.4142, RISE_DIAG, -1, -nx))

    def step(self, deadline: float) -> bool:
        """Expand the search until perf_counter() passes `deadline`. Returns True when finished."""
        if self.done:
            return True
        g = self.g
        blocked = g.blocked
        near = g.near
        surf = g.surf
        nx = g.nx
        dist = self.dist
        heap = self.heap
        pop = heapq.heappop
        push = heapq.heappush
        nbrs = self.nbrs
        clock = time.perf_counter
        count = 0
        while heap:
            count += 1
            if (count & 127) == 0 and clock() > deadline:
                self.pops += count
                return False
            d, u = pop(heap)
            if d > dist[u]:
                continue
            hu = surf[u]
            for off, cost, rise, sa, sb in nbrs:
                v = u + off
                if blocked[v]:
                    continue
                if sa and (blocked[u + sa] or blocked[u + sb]):
                    continue
                # we walk from v to u; climbing more than `rise` metres in one cell is not routed
                if hu - surf[v] > rise:
                    continue
                nd = d + cost * CELL * (NEAR_COST if near[v] else 1.0)
                if nd < dist[v]:
                    dist[v] = nd
                    push(heap, (nd, v))
        self.pops += count
        self.done = True
        return True

    def value(self, c: int) -> float:
        if 0 <= c < self.g.n:
            return self.dist[c]
        return INF

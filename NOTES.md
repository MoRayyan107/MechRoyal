# NOTES

## What we know (from the kit, 21 Nov 2026)

- No simulator in the kit. `check/check.py` replays 300 recorded observations: protocol and timing only.
  Real scores come only from submissions (48 matches each, noisy, so compare means, not small differences).
- Score = mean of `16 - place`. House bots never fight each other and hunt the weakest first,
  so points come from house bots dying before us (to us, the zone, or drones).
- The zone closes to radius 0 at about 6:55 (sum of the phase hold + shrink times = 415 s).
  After that everyone takes 260 HP/s, so the final order is decided by HP: max-HP cubes and upgrades matter.
- Regen: 45 HP/s after 5 s without dealing or taking damage (not outside the zone). Not shooting keeps regen on.
- Rear hits do x1.5: facing the attacker matters.

## Plan (one change per submission)

0. Robust base: stdlib Python, pinned image, per-tick try/except with fallback, stress test, route checker.
1. Never die to the zone: 4 m grid, distance field to the next circle, boost when time is short, jet when stuck.
2. Avoid fights: cover, scanning, backing off, facing attackers.
3. Safe looting; max HP and armour upgrades first.
4. Cheap shots with lead and the right weapon for the range.
5. Endgame plan.

## Design choices (step 0 + 1)

- **Grid**: 4 m cells over the whole map (269 x 183). A cell is blocked if a building/stadium solid stands on its
  centre (unless it is walkable on top, low deck or bridge, or high enough to walk under). Cells within a mech
  radius of a wall are walkable but cost 2.5x, so routes keep off walls. Built once in about 0.7 s at start-up.
- **Distance field**: Dijkstra from every walkable cell inside the goal circle; it covers the whole map in ~60 ms,
  so it is computed in 6 ms slices across ~10 ticks whenever the goal changes. Until it is done we walk straight.
- **Goal**: the next zone circle shrunk by a margin (min(20 m, 30% of its radius), at least 5 m).
  Inside the next circle we are inside every circle of this phase (the next circle is contained in all the
  interpolated ones), so once there we stop and scan.
- **Boost**: when outside the zone, or when walking time + 4 s exceeds the time until the moving edge reaches us.
- **Stuck**: commanded to move but moved < 2.5 m in the last second: jump + jet for 1.6 s with a sidestep that
  alternates straight / left / right. This also gets us off roofs and over ledges the grid does not model.
- **Defence**: on damage, aim at the attacker (or the damage bearing if unseen) for 4 s; fire the gun with lead
  only if the actual aim is on target and no building is in the way. Mechs within 45 m are also engaged.
  We do not shoot otherwise (sound gives us away and shooting stops regen).
- **Upgrades**: plating, armor, nanites, servos, sensors, ... (survival first).
- **Never crash**: `decide()` is wrapped; on any exception a minimal fallback (walk to the next zone centre,
  boost if outside) answers. Missing map files leave the bot on straight-line moves.

## Design choices (sub-02: hide, move quietly, hold fire)

Diagnosis after sub-01 (1.79, i.e. dying about 14th): most likely killer is the house-bot squad finding us standing
in the open (sub-01 stopped at the first cell inside the circle, usually open ground). Confidence ~65%; no death
telemetry exists. protocol.md says nothing about house bots: "weakest first" is not defined in the kit, so we cannot
rely on HP making us a less attractive target; staying unseen and alive is what we can control.
Ranking of planned steps by expected gain: step 2 (hide) > step 5 (endgame, but only pays if alive at ~415 s) >
step 3 (looting: needs shooting crates or drones, noisy, stops regen). Step 2 has medium risk, step 3 the highest.

- **Cover map** (built at start-up, adds ~0.05 s): per 4 m cell, canopy = sum of (crownR - distance) over tree crowns
  spanning 3-9 m above our feet (a sight line through ~4 m of foliage is blocked), capped at 6; plus 0.4 per tall
  building cell (roof above mech height) within 2 cells, capped at 4. Trees do not stop shots, only sight.
- **Hiding spot**, picked each time the zone announces a new circle: the walkable cell with the highest
  cover - (distance from us / 40 m), within 80% of the goal radius and 120 m of us (or of the circle point nearest
  us), at least 55 m from any drone we have seen (they aggro at 45 m). Re-picked if a drone shows up within 55 m of
  the spot (at most every 10 s). The distance field now leads to the spot; once on it we stand still and scan.
- **Quiet walking**: footsteps are only made above 5 m/s, so we walk at 4.5 m/s when walking that slowly still beats
  the zone edge by 15 s and nothing has hit us in the last 4 s. Otherwise full speed; boosting rules unchanged.
- **Hold fire**: no more engaging mechs just for being within 45 m. Fire only at whoever hit us in the last 4 s,
  or a nearly free kill (visible mech with HP <= 2 gun hits, within 100 m, clear line). Keeps regen on and us silent.
- **Endgame**: when the next radius is <= 25 m, the spot is the circle centre (within 8 m), ignoring cover: everyone
  is inside sensor range then and the centre stays in the zone longest. If the centre is inside a building we walk at
  it and the stuck rule jumps/jets us onto the roof. Upgrade order still starts with plating and armour.
- Not in this change: looting (step 3).

## How we read score changes

Practice seeds are fixed, so the same bot meets the same 48 matches; a difference is mostly caused by our change,
but small behaviour changes cascade through a match, so it is still noisy.
- +-0.1: no real effect. Keep whichever version is simpler.
- +-0.3: probably real. Keep it if up; if down, look for the cause before stacking another change on it.
- +-0.5 or more: clearly real. If down, revert to the previous version and rethink.

## Offline test results

| Commit | tools/stress.py | check.py local | check.py container (sandbox flags) |
| --- | --- | --- | --- |
| sub-01 (step 0+1) | PASS: replay p99 6.2 ms, fuzz max 7.4 ms, routes 120/120 | PASS p99 6.4 ms | PASS p99 2.0 ms, max 2.1 ms |
| sub-02 (hide) | PASS: replay p99 6.2 ms, fuzz p99 6.3 / max 7.1 ms, routes 114/120 to the spot (misses: walker stuck in pockets it cannot jet out of) | PASS p99 6.4 ms | PASS p99 1.3 ms, max 1.4 ms (twice) |

## Submissions

| Tag | Commit | Change | Score (48 matches) |
| --- | --- | --- | --- |
| starter | (kit example) | walk to zone centre, shoot everything | 1.15 |
| sub-01 | 68ec13514c315d95aa517df00a554491d1ca183e | step 0 + 1: zone routing, stuck recovery, return fire, survival upgrades | 1.79 |
| sub-02 | see `git rev-parse sub-02` | hide in cover, walk quietly, hold fire, endgame at the centre | pending |

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

## Offline test results

| Commit | tools/stress.py | check.py local | check.py container (sandbox flags) |
| --- | --- | --- | --- |
| step 0+1 | PASS: replay p99 6.2 ms, fuzz max 7.4 ms, routes 120/120 | PASS p99 6.4 ms | PASS p99 2.0 ms, max 2.1 ms |

## Submissions

| Tag | Commit | Change | Score (48 matches) |
| --- | --- | --- | --- |
| starter | (kit example) | walk to zone centre, shoot everything | 1.15 |
| sub-01 | see `git rev-parse sub-01` | step 0 + 1: zone routing, stuck recovery, return fire, survival upgrades | pending |

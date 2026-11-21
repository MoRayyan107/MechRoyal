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

## Early-death investigation (after sub-02 = 1.69)

1.79 vs 1.69 is noise; both mean dying ~14th, i.e. early. What we can observe: only the board score (and stderr tail
on a rejected submission). stderr, debug drawings, per-match place/kills never reach us; house-bot behaviour is not
documented. The kit's only match data is check/stream.jsonl.gz: slot 3 spawns at (320,-280), sees mech 10 at 139 m
at t=4.9 s, mech 10 has a laser by t=7.9 s and kills it from 55-150 m by t=36.8 s (place 13), before the zone moves.
No drones seen in 37 s. Spawns are >= 130 m apart but vision is 200 m and laser range 280 m.
sub-02 from that spawn walks 181 m at 4.5 m/s (40 s), all of it within 200 m of mech 10, 42% on open ground.
Hypotheses: H1 spawn neighbour snipes us in its line of sight (~60%); H2 quiet walking lengthens exposure (~15%);
H3 we lose duels with only the gun (~15%); H4 drones (~7%); H5 facing/return fire (~3%); H6 zone (<2%).

## Design choices (sub-03: break line of sight with buildings) - tests H1

New module `losbreak.py`, wired into bot.py after the sub-02 move is computed; every other sub-02 rule unchanged.
- **On** while an enemy mech is in obs.mechs (the server keeps listing a mech 5 s after it leaves view) or a mech
  (id < 100) hit us in the last 4 s. **Off** otherwise, and also whenever the zone is pressing (outside the zone, or
  the sub-02 boost condition is true): the zone wins.
- Threats: nearest 3 enemy mechs within 300 m (laser range 280). A mech that hit us but is not listed is placed
  120 m away along the damage bearing.
- If where we stand is hidden from every threat: stand still, torso facing the nearest threat (front armour).
  Otherwise walk straight, at full speed (boost if energy > 30), to the nearest cell within 48 m that is hidden from
  every threat, reachable in a straight line over walkable cells, and inside the zone circle 5 s from now (3 m margin).
- **48 m search distance**: about 4 s walking (2.5 s boosting), i.e. at most 1-2 laser shots (one every ~2.4 s) while
  we cross; farther cover costs more exposure than it saves. With nothing in reach, sub-02 movement runs unchanged
  (re-searched every 0.5 s).
- **Hidden** = a building cell within 40 m of us along the line to the threat has a roof above the line from the
  threat's eye (z+8) to the top of our mech (z+9, scaled by cubes). Blocking the top line also blocks the line to our
  centre (it runs lower everywhere near us). Also required 2 m to each side of each line (our radius is 2.5 m).
  Trees never count: they stop sight, not shots.
- Cost: cells nearest-first with a 5 ms per-tick budget (resumes next tick); own try/except, so a bug there falls back
  to sub-02 movement; decide() still has the per-tick fallback.
- Also in this commit (speed only, same result): sub-02's spot picker tests drones only for would-be winners and
  ignores drones far outside its search area. It caused the only answers over 12 ms in the fuzz test (up to 14 ms).
- How we read the result: **+0.3 or more over ~1.7 means H1 was right; within +-0.1 it was wrong and H3 (pick up a
  laser near spawn) is next.**

Recorded-match check (crude walker, mech 10 on its recorded track): starting at the recorded position at t=4.9 s,
sub-03 runs 14 m to (290,-255) and is hidden after 0.6 s (in mech 10's line 0.6 of 25 s, walked 23 m); sub-02 stays
in its line 24.1 of 25 s while walking 112 m. Caveat: the same 4 m roof-height model decides and measures "hidden".

## Design choices (sub-04: pick up a better weapon) - tests H3, base = sub-01

sub-03 scored below sub-02: hiding and breaking line of sight did not help, so avoidance rules stop here.
**Base:** sub-01. Commit 61951f51a9bb36a1a34bab95618596f96cd5759c on main restores sub-01's bot.py, nav.py,
Dockerfile and .dockerignore (byte-identical to tag sub-01) and removes losbreak.py; sub-02 and sub-03 stay in main's
history untouched. sub-04 is the next commit on top: new `loot.py` + weapon-aware firing in bot.py.

Weapon pickups (protocol.md 6, 7): explicit `take: <id>` within 4 m horizontal / 4 m vertical; goes to the empty slot
(slot 1 at spawn) or, with `drop: <slot>`, swaps; no two of a kind; a taken weapon cannot fire for max(1 s, its
cooldown) (laser 1.65 s). Laser: hold to charge (0.15-0.75 s, 35-100% of 315), release fires a hitscan beam along the
actual aim, 280 m, cooldown 1.65 s, half speed while charging, visible/audible. Lightning: instant, nearest target
within 45 m and 30 deg of aim yaw with line of sight, 60 dmg, chains x2, 3 energy, 0.25 s. Mortar not fetched.
Unclear in protocol.md: whether the 30 deg cone is +-15 or +-30 deg; we aim straight at the target.

- **Loot rule ON** only if: no enemy mech listed in obs.mechs; no mech hit us in the last 4 s; we are inside the zone
  and sub-01's boost condition is off; a visible pickup better than our slot 1 (laser > lightning > empty) lies within
  **60 m** (about 5 s walk; other mechs spawn >= 130 m apart, so this stays in our own spawn area), on walkable ground
  (not a roof: <= 2.5 m above the cell's walking height), reachable in a straight walk, no drone seen within 45 m of
  it, inside the zone circle 5 s after we would arrive (10 m margin), and the zone edge reaches it no sooner than
  walk there + 1.5x walk back to the goal circle + 20 s. Then walk straight at full speed and send `take` in reach
  (`drop: 1` when swapping lightning for a laser). Give up for good after 2x the walk time + 4 s, or on take_failed
  gone/have/full/busy.
- **OFF** otherwise: the move is exactly sub-01's.
- **Firing:** same target choice as sub-01 (attacker in the last 4 s, else any mech within 45 m). Lightning if the target
  is within 90% of its range (aim straight at it, fire when within a quarter of the cone, line clear, energy >= cost +
  10); else the laser if within 95% of its range and ready or charging (aim straight, no lead: hitscan; hold to charge,
  release when fully charged and on target; a clear line needed to start); else the gun exactly as sub-01. The gun
  also fires in the same tick if its own lead solution is on target.
- **How we read the result:** +0.3 or more over 1.79 means H3 was right; within +-0.1 it was wrong.

Recorded match (crude walker): of the nine pickups seen in 37 s, the four near spawn (8007 lightning 46 m, 8006
lightning 78 m, 8001 laser 79 m, 8003 laser 133 m) are on rooftops 13-24 m up; the nearest ground pickup (8022 laser)
was 134-151 m from the bot. So sub-04 takes nothing in that match and moves exactly like sub-01 (108 vs 109 m walked
by t=36.8). With a synthetic laser on open ground 40 m from that spawn, sub-04 takes it after 3.2 s and resumes
sub-01's route; with a mech in view the rule stays off. Rough estimate from the loot numbers: ~0.3 laser/lightning
pickups on open ground within 60 m of a random point, so the rule fires in a minority of matches.

## sub-05: ground pickup radius 60 m -> 80 m (base sub-04 = 1.98, the best so far)

Only `loot.LOOT_M` changes. All gates as sub-04. Worst case: ~6.8 s out instead of ~5.1 s; give-up after
2 x walk + 4 s (~17.7 s at 80 m). Guess: a far-side pickup can take us 50-80 m closer to a spawn neighbour before it
comes into view. Recorded match: no change (nearest ground pickup 134 m). Synthetic: a laser 70 m away on open ground
is taken at t=5.7 s by sub-05 and ignored by sub-04. Reading: +0.3 over 1.98 = keep; within +-0.1 = noise, keep only
if we have nothing better (it is harmless by construction); below = drop and go back to sub-04.

## sub-05b: loot bug fixes on sub-05 (from the code review; no new rule)

- #2 give-up timer: counts the seconds actually spent fetching each pickup (per pickup id), so a pickup that blinks
  out of view, or a rule that flickers off, no longer resets it. CAN change play: a fetch that never gets closer now
  ends after its budget (2 x walk + 4 s) instead of possibly never. Staged test: blinking pickup, sub-05 chased it
  238 ticks in 60 s and never gave up; sub-05b gave up after 12.5 s.
- #6 take results: `took` / `take_failed` are read on every tick, also when the zone skips the loot rule. CAN change
  play: a failed take ("have"/"full"/"busy"/"gone") seen on a zone-pressing tick now bans that pickup; before, the bot
  could walk back to it. Staged test confirms.
- #8 README says 80 m. Cannot change play.
- #9 removed the dead "laser charging" check: with a laser in slot 1 no pickup outranks it, so the loop returned
  "nothing near" with the same result (None, state off). Cannot change play.
- Normal path identical: 0 differing actions vs sub-05 on the 300 recorded ticks and 3000 fuzz ticks.
- Submitted: scored 2.31, the same as sub-05.

## sub-07: long laser shots at a lone mech (option A) - base sub-05b (2.31)

Before sub-07 the laser already fired out to ~266 m, but only at a mech that hit us in the last 4 s (or within 45 m).
sub-07 adds shots at a mech that has NOT hit us:
- ON only if: we hold a laser that is ready (cooldown <= 0.2 s) or already charging this shot; exactly one enemy mech
  listed in obs.mechs; it is 45-240 m away (vision is 200 m, so that is the practical limit; 240 leaves margin under
  the 280 m laser range); listed without a break for >= 0.5 s; clear line by our building model; inside the zone and
  the zone boost condition off.
- Never starts a new charge with two or more mechs in view. sub-05b's attacker and within-45 m rules come first and
  are unchanged. The gun (and lightning) never fire at a sub-07 target.
- Full charge (0.75 s, 315 dmg, ~131 dmg/s vs ~61 for minimum charges). Start charging when aimed within 0.3 rad,
  release when full and on target. Target leaves the line mid-charge: keep aiming at it (its last known position if
  no longer listed) and release if the line clears; otherwise the server fires it on its own after 2 s held.
- Off otherwise, always off without a laser: identical to sub-05b.
- Edge cases (tested): laser in either slot works; after a take the equip delay shows as cooldown, so no charge until
  <= 0.2 s; a charge in progress when the zone starts pressing is released (beam fires) and we move at full speed.
- Recorded match (laser assumed in slot 1, ASSUMING SHOTS HIT): sub-07 starts charging at t=5.7 s and fires a full
  charge at 6.4 s, 1.5 s before mech 10's first laser hit (7.9 s); then sub-05b's attacker rule fires at 8.9, 11.4 s as
  before. Cost seen: at 18.9 s and 24.6 s it starts charges on a clear line that becomes blocked (20-26 s), and both are
  auto-fired into the wall (21.5, 27.2 s): two wasted, noisy shots.
- Reading the result vs 2.31: +0.1 or more = worth keeping; within +-0.1 = no effect; below = fall back to option B
  (fire only at a mech not facing us) as sub-08.
- RESULT: 2.27, no effect. Base stays sub-05b. sub-07's code stays in history; main's bot.py was restored to sub-05b's
  (commit after 30afa6b) before sub-08.

## sub-06 design (rooftop weapons by jump + jets) - ON FILE, NOT BUILT

Reachable roof: pickup cell is a building whose roof height matches the pickup z within 1 m; roof region = connected
cells at that height within 30 m; take-off = open cell next to that region nearest us with a straight walk; climb =
roof height - ground at take-off <= 25 m; every cell from take-off to pickup is that roof (no higher section, no gap);
energy >= 2 x climb + 30 (guess). Up: walk to take-off, push toward the pickup, jump + jet until feet are 1.5 m above
the roof, stop jets over the roof, walk to the pickup, take within 3.5 m horizontal / 3.5 m vertical; remember the
pickup position when it is out of sight. Gates: same as the ground rule + energy + 4 s extra in the zone check + at most
2 rooftop attempts per match; ground pickup preferred unless the roof one is much closer (+1.5 x climb in the cost).
Give up: energy < 8 below the roof, no height gain for 1.5 s while jetting, back on the ground after jetting (2 fails
ban it), time > 2 x walk + 8 s, or a mech appears. Down: sub-01 routing walks off roofs; backstop after 10 s on a roof:
walk back to the take-off edge. Recorded match: 8007 (climb 11 m) and 8001 (15 m) qualify; 8006 fails (23 m section in
the way); 8003 is beyond 80 m. Estimated to fire in ~10-20% of matches; expected gain +0.1 to +0.2 (inside the noise).

## Remaining plan (until Sunday ~1:00 PM UK)
sub-06 rooftop weapons by jump + jet; sub-07 laser fire at visible mechs out to ~220-260 m; sub-08 early duel with
the spawn neighbour (propose first). Each based on the best-scoring tag so far.

## Offline test results

| Commit | tools/stress.py | check.py local | check.py container (sandbox flags) |
| --- | --- | --- | --- |
| sub-01 (step 0+1) | PASS: replay p99 6.2 ms, fuzz max 7.4 ms, routes 120/120 | PASS p99 6.4 ms | PASS p99 2.0 ms, max 2.1 ms |
| sub-02 (hide) | PASS: replay p99 6.2 ms, fuzz p99 6.3 / max 7.1 ms, routes 114/120 to the spot (misses: walker stuck in pockets it cannot jet out of) | PASS p99 6.4 ms | PASS p99 1.3 ms, max 1.4 ms (twice) |
| sub-03 (break LOS) | PASS on seeds 1, 2, 4: fuzz p99 8.0-8.2 ms, max 8.6-9.5 ms; replay p99 6.2 ms. Seed 3 routes 36/40, identical to sub-02's own code on that seed. Fault injection: 300/300 answered | PASS p99 6.4 ms | PASS p99 1.2-1.5 ms, max 1.8 ms (twice, built from 67ce118) |
| sub-04 (loot, on sub-01) | Seeds 1-3 PASS: fuzz p99 6.5-9.0 ms, max 7.2-14.1 ms (the slow ticks are sub-01's own new-circle setup; loot code max 0.09 ms); replay p99 6.2-6.4 ms. Default seed: routes 56/60 = FAIL at the 95% bar, identical for the restored sub-01 bot with the same test file (walker pockets). Injected loot crash: 300/300 answered with sub-01 moves | PASS p99 6.5 ms | PASS p99 1.1-2.0 ms, max 2.1 ms (twice, built from 25e5e53) |
| sub-05 (loot 80 m) | Seeds 1-3 PASS: fuzz p99 6.3-6.6 ms, max 7.4-13.2 ms; replay p99 6.2-6.4 ms. Default seed routes 56/60, same as sub-04 and the sub-01 bot. Injected loot crash: 300/300 answered | PASS p99 6.4 ms | PASS p99 1.2-1.9 ms, max 2.1 ms (twice, built from bd9167c) |
| sub-05b (loot fixes) | Seeds 1-3 PASS: fuzz p99 6.3 ms, max 6.8-10.0 ms; replay p99 6.2 ms. Default seed routes 56/60 (same as sub-05). Injected crash in loot update + read_events: 300/300 answered | PASS p99 6.5 ms | PASS p99 1.2-1.5 ms, max 1.8 ms (twice, built from 7b2029d) |
| sub-07 (laser long shots) | Seeds 1-3 PASS: fuzz p99 6.3 ms, max 6.7-8.8 ms; replay p99 6.1-6.2 ms; lone-mech scenario p99 0.13 ms, max 6.3 ms. Default seed routes 56/60 (same as sub-05b). Side by side vs sub-05b, no laser: 0/3000 random ticks differ; recorded 300 ticks 0 differ with the route field pre-finished (1 tick differs on 1 run in 3 for sub-05b vs itself too: wall-clock field slicing). Injected crash in the new rule: 300/300 answered | PASS p99 6.4 ms | see report |

## Submissions

| Tag | Commit | Change | Score (48 matches) |
| --- | --- | --- | --- |
| starter | (kit example) | walk to zone centre, shoot everything | 1.15 |
| sub-01 | 68ec13514c315d95aa517df00a554491d1ca183e | step 0 + 1: zone routing, stuck recovery, return fire, survival upgrades | 1.79 |
| sub-02 | a62d9a5a81595c0786c17316eade10e300ded656 | hide in cover, walk quietly, hold fire, endgame at the centre | 1.69 |
| sub-03 | 67ce118653ddc37c23113584e408cd30c6171c73 | break line of sight with buildings while a mech sees us / just hit us (tests H1) | below 1.69 (exact score not reported) |
| sub-04 | 25e5e53314c303c3df1be0db3c67e671003e61be | base sub-01 + pick up a laser/lightning within 60 m when safe, fire the weapon that suits the range (tests H3) | 1.98 |
| sub-05 | bd9167c47aaa8d41cfef977231f9340f96eb93e8 | sub-04 with ground pickup radius 80 m | 2.31 (best so far, new base) |
| sub-05b | 7b2029dfe317aa58cc33b4c35dcf09b4e9780c95 | sub-05 + loot fixes #2 #6 #8 #9 | 2.31 (same as sub-05; base for sub-07) |
| sub-07 | 30afa6bec52de8b80454565967d8a9a224c92de6 | sub-05b + laser shots at a lone mech at 45-240 m (option A) | 2.27 (no effect vs 2.31; not built on) |

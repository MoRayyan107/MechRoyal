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

## sub-09: get cubes (base sub-05b = 2.31)

Why: our bot never collected cubes, so its upgrade code almost never ran. One cube = +4% max HP and damage and, at
1 cube, an upgrade offer (plating first: about +20% HP in all). Cubes come from crates (1 each, plus a weapon half
the time), drones, and kills (the dead mech's cubes + 2, dropped on the ground).
- Gates (same as the weapon rule): no enemy mech listed; no mech hit in the last 4 s; inside the zone and the zone
  boost condition off. Weapons outrank cubes, cubes outrank crates.
- Cube: walk onto the nearest visible cube within 60 m whose spot is open walkable ground (not a roof, not a
  footprint), reachable by a straight walk, no drone within 45 m of it, with the same zone check as weapons
  (t_out >= there + 1.5 x back + 20 s; inside the circle at arrival + 5 s with 10 m margin). Collected automatically
  within 3.5 m (no take). Give-up timer counts only time spent on that cube (2 x walk + 4 s).
- Crate: the nearest visible ground crate within 60 m whose spot passes the same cube checks, with a clear line by our
  building model, and no hit of any kind in the last 4 s. A new crate needs >= 9 rounds; at most 4 crates started per
  match; give up after 6 s on one crate. We aim the gun at it (crate centre, tolerance 1 m) and fire, and the legs
  walk TOWARD the crate (stopping within 8 m), because its cube drops there. Changed from the first description
  ("shoot while walking the route"): the synthetic test showed the route carrying us from 45 to 62 m away, out of
  range, with the crate at 30 HP and its cube beyond the 60 m cube limit.
- Reload early: within 5 s of a crate shot, reload when the magazine is below 6 (the base rule reloads below 4).
- Crash fallback now also answers an upgrade offer (before: a crash in decide meant the 5 s timeout picked choice 0).
- Off otherwise: exactly sub-05b (tested: 0 differing actions when no crate or cube is visible).
- Sound (guess): the squad shares vision and already hunts us, so we assume gun (320 m) and crate (220 m) noise adds
  little; the kit does not say whether house bots react to sound. Cap: 4 crates a match.
- Worst case: cube detour up to ~5 s out (give-up after ~14 s); crate: walk up to ~52 m toward it (~4.5 s) and then
  onto the cube; ~7-10 rounds used and a 2.4 s reload during which the gun cannot fire; a hunter that appears
  mid-detour turns the rule off at once (it is listed), and the bot is sub-05b again, but our torso may be facing the
  crate/cube, so a first hit from behind takes x1.5.
- Synthetic tests (open ground, assumes on-target shots hit): cube 40 m -> collected at 3.1 s, offer [sensors, armor,
  plating] -> plating, back on the route at 3.3 s. Crate 45 m -> first shot at 0.1 s walking toward it, destroyed at
  1.6 s after 7 shots (5 rounds left), early reload, cube collected at 3.5 s, plating picked, back on the route at
  4.2 s with a full magazine.
- Reading the result vs 2.31: +0.1 or more = keep; within +-0.1 = no effect; below 2.2 = drop it.
- RESULT: 2.48 (+0.17). Keep: new best and base for sub-10.
- Observation: collecting cubes and upgrading helped; weapon pickup by radius was saturated at 80 m.

## sub-10: cube and crate radius 60 m -> 80 m (base sub-09 = 2.48)

One change: loot.CUBE_M and loot.CRATE_M 60 -> 80 (plus the doc lines quoting them). 4-crate cap, 9-round start and
every gate unchanged.
- Risk (guess-level spawn model, as for sub-08): a spot 60-80 m away is closer to another spawn than to us in ~4% of
  cases, within 70 m of another spawn ~3%, within 45 m ~0% (the rejected 80-100 m weapon band was 25%). Noise is made
  where WE stand (gun 320 m, crate 220 m) whatever the crate distance, so 80 m adds no louder shots, only more crates in
  range; limits: 4-crate cap, no mech in view, no hit in 4 s, rule off the moment a mech is listed.
- Worst case: cube at 80 m = 6.8 s out, give-up after 2 x 6.8 + 4 = 17.7 s; crate at 80 m = ~6 s walking toward it while
  firing (gun spread at 80 m ~0.8 m vs crate radius 1.4 m), crate give-up 6 s unchanged, then its cube. The zone check
  scales with distance (there + 1.5 x back + 20 s) and still holds.
- Chosen 80 m over 70 m: a fresh lever that just gained +0.17, a low-risk band, +78% area for a clearer signal.
- Fires (guess): ground crates in range 0.46 -> 0.88 on average, i.e. at least one in ~59% of matches instead of ~37%,
  before visibility losses; cubes scale the same way.
- Reading the result vs 2.48: +0.1 or more = keep 80 m; within +-0.1 = no effect, stay on 60 m; below 2.38 = drop it.
- RESULT: 2.44, no effect. Drop 80 m; cube and crate radius stays 60 m (sub-09). sub-10 stays in history; main's bot
  files (bot.py, loot.py, README.md) were restored byte-identical to tag sub-09 in the next commit, as the base for sub-06.
- Observation: cube/crate pickup by radius is saturated at 60 m, like weapons at 80 m.
- The sub-10 Docker check result was never written into the table below ("see report"); the session that ran it was lost.

## sub-06 design (rooftop weapons by jump + jets) - original design (built: see the next section)

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

## sub-06: rooftop weapons by jump + jets (base sub-09 = 2.48) - BUILT 21 Nov

New class `loot.RoofLooter`, wired into bot.py after the ground weapon rule. No new file in the image.
- **Candidate** (each tick, only when no ground pickup is closer): a visible laser/lightning better than slot 1, within
  80 m, whose 4 m cell is a building whose roof height is within 1 m of the pickup z. Roof = connected cells at that
  height within 30 m. Take-off = an open cell next to that roof, with the next cell further out also open at the same
  height (>= 8 m of open ground: not a narrow gap between two roofs, which the 5 m wide mech bridges), climb 1.2-25 m,
  and the 1 m-sampled line from it to the pickup stays on that roof (no gap, no higher or lower section). We must reach
  the take-off by a straight walk; energy >= 2 x climb + 30; no drone within 45 m of pickup or take-off; zone check =
  the ground rule's (t_out >= there + 1.5 x back + 20 s) + 4 s, with there = walk + roof walk + climb/8 + 1 s, at both
  the pickup and the take-off, and both inside the circle 5 s after arrival with a 10 m margin.
- **vs ground pickup:** the roof one wins only if walk + roof walk + 1.5 x climb < the ground pickup's distance. If it
  wins, the ground rule's give-up timer for that tick is undone (bot.py restores looter.spent / prev_t).
- **Gates, every tick, also mid-attempt:** no mech listed; no hit of any kind (drones too) in the last 4 s; inside
  the zone and the zone not pressing (bot.py does not call the rule then). Any gate fails -> None that tick, the bot is
  sub-09; an attempt in the air is over (it was counted) and may restart from the take-off once the gates clear.
- **Attempt:** walk (no boost) to within 3 m of the take-off; if energy < need, stand and regen; then push along the
  checked line, jump, jet until the feet have been 1.5 m above the roof; then coast toward the pickup, jetting again
  only if we sink below roof + 1.5 m before we are over the roof; landed = grounded with feet >= roof - 0.8 m. Walk to
  the pickup and `take` within 3.5 m (with `drop: 1` if slot 1 holds lightning). The pickup position is remembered
  while it is out of sight (from below, a roof pickup is usually hidden by the roof edge).
- **Fail** (bans the pickup, counts toward 2): no height gain (0.3 m) for 1.5 s while still below roof + 1.5 m;
  energy < 8 below the roof; grounded more than 2 m below the roof >= 0.3 s after the jump (or after landing); out of
  time (2 x walk + 8 s, counted only on active ticks) after a jump. Out of time before a jump = give up, not a fail.
  On the roof and the pickup not listed within 8 m = gone (ban, not a fail). At most 2 jumps per match; 2 fails = off.
- **Stuck rule:** sub-01's stuck rule is unchanged everywhere EXCEPT during the jet phase itself (at most ~1.5-3 s),
  where it is paused: jetting up a wall moves us < 2.5 m sideways per second, so it would fire after 1 s and sidestep
  us 40 deg off the line. The rule's own "no height gain for 1.5 s" check replaces it there.
- **Backstop:** armed at landing and after any failed jump. Still standing high (feet > 1.5 m above the cell's walking
  height, within 50 m of the pickup) 10 s later, with the gates open and nothing else to do: walk back along the
  checked line toward and past the take-off point until off. Ends when off, or 25 s after landing.
- **Off:** None = the caller's sub-09 code path, unchanged.

Jump-and-jet simulator (`tools/jetsim.py`, OUR model, not the game): 20 ticks/s; numbers from hello.config.mech and
protocol 7 (walk 11.7, boost 19.5 on the ground, accel 58.5 / 12 in the air, gravity 22, jump 11, jets +34 capped at
10 m/s, 15 energy/s, regen 18/s after 0.6 s, step-up 1.2 m, walls slide, ceilings stop jets, roof-edge landing and
0.6 m/tick slide-off); the exact building prisms from world.json; vision = protocol 8 (eye z+8 to the pickup centre or
top, 45 m any way, 200 m in 120 deg). GUESSES: air control aims at walk speed; wall contact = 12 points on the 2.5 m
circle + centre; pickup centre 0.4 m / top 0.8 m above its pos; torso turns 0.4 rad per decision; no mechs pushing,
no slope limit; roof spawn uniform over roof area. Check: 100 energy climbs 69 m in our model vs the protocol's
"about 60 m": the model is ~15% optimistic on climbing; the energy need (2 x climb + 30) has ~30% margin over our cost.

Results (offline, our model):
- Recorded match, from its spawn, pickup listed every tick: 8007 (lightning, roof 12.8 m, climb 12.7 m): jump 1.3 s,
  landed 3.9 s, taken 5.2 s, back on the ground 7.2 s (sub-09 routing walked off). 8001 (laser, 17.4 m, climb 14.6 m):
  jump 7.7, landed 10.5, taken 11.2, ground 14.9 s. 8006 and 8003: no plan (higher section / beyond 80 m). With our
  vision model none of the four is ever seen from that spawn (roofs above the eye), and mech 10 is listed from 4.9 s,
  so in that match the rule never fires.
- Random sweep (5 seeds x 200 rooftop pickups, starts 15-80 m away on open ground, pickup listed every tick, static
  zone): 409 committed, 366 taken (89%); fails: 34 no height gain (mostly 3-6 m climbs into a section the 4 m grid does
  not show, or an overhang over the take-off), 6 out of time, 4 back on the ground; 6 runs (1.5%) end the 45 s still
  standing high (sub-09 then stands there until the zone moves; its routing walks/jets off then). Median take 5.9 s
  after start; median take -> ground 12 s (the 10 s backstop wait: inside a static zone sub-09 just stands).
- Same with our vision model (3 seeds x 200): 99 committed (17%), 86 taken; same failure kinds.
- How often it fires (GUESS-level, `jetsim.py freq`): ~6.7 roof laser/lightning per match; from a spawn-like spot the
  chance of at least one qualifying AND visible within 80 m is ~2.6%; with walking and scanning before a mech is seen,
  perhaps 3-6% of matches. The old estimate (10-20%) ignored that a roof pickup is hidden from below. Expected effect
  on the score: small, very likely inside the +-0.1 noise even if every fetch helps.
- Gates (`jetsim.py gates`, 8007 case): mech listed mid-jet -> off that tick, then identical to the rule disabled
  (same trajectory); mech gone after 2 s -> second attempt succeeds; hit mid-jet -> off 4 s, second attempt fails
  (back on the ground), rule done; mech from the start -> never commits; crash mid-jet -> 300/300 answered, rule off.
- Cost per tick: the rule itself max 0.26 ms (roof planning included, cached per pickup); fuzz 0.07 ms.
- Worst case (guess): walk <= 80 m to the take-off (~7 s), up to 2 jumps of ~3 s each at ~1.5 energy/m (a 25 m climb
  uses ~45 of >= 80 energy), ~10-15 s standing on a roof after the take before the backstop; a mech arriving mid-jet
  sees us in the air with jets audible at 90 m (sound GUESS: not checked against house-bot behaviour), and we fall.
  Budget limit per pickup: 2 x walk + 8 s of active time.
- Reading vs 2.48: +0.1 or more keep; within +-0.1 no effect; below 2.38 drop.

## sub-08: ground weapon pickup radius 80 m -> 100 m (base sub-05b = 2.31)

One change: loot.LOOT_M 80 -> 100 (plus the two doc lines that quote it). Everything else is sub-05b.
- Risk (guess-level spawn model: random walkable spawns inside 0.8 x start radius, >= 130 m apart): a pickup 80-100 m
  away is closer to another spawn than to us in ~25% of cases (60-80 m band: 4%); within 70 m of another spawn 11%;
  within 45 m 1%. 80-90 m: 18%; 90-100 m: 31%. Limits: our 200 m / 120 deg view points where we walk, so a neighbour
  near the pickup is usually seen and the rule turns off at once (unless buildings/trees hide it - guess); a hit in
  the last 4 s turns it off; zone gates unchanged; give-up after 2 x walk + 4 s.
- Worst case: 8.5 s walk out, give-up after ~21 s of fetching; zone check unchanged (t_out >= there + 1.5 x back + 20 s,
  inside the circle at arrival + 5 s with a 10 m margin, skipped while the zone presses).
- Chosen: 100 m (a bigger step gives a clearer signal against +-0.1 noise). 90 m is the fallback if 100 m lands at
  2.2-2.4 (half the added area, lower neighbour risk: 18% vs 25%).
- Fires (guess): expected ground laser/lightning candidates ~0.53 at 80 m -> ~0.83 at 100 m, i.e. roughly 55% of
  matches instead of 40%, before visibility losses.
- Reading the result vs 2.31: +0.1 or more = keep 100 m; within +-0.1 = no effect, stay on 80 m; below 2.2 = 100 m is
  too far, drop it.
- RESULT: 2.27, no effect. Drop 100 m, do not build on it; the radius stays 80 m (sub-05b). sub-08 stays in history;
  main's bot files were restored to sub-05b's in the next commit.
- Observation: weapon acquisition by radius is saturated: 60 -> 80 m gave +0.33, 80 -> 100 m gave nothing.

## Corrections after the rules check (21 Nov 2026, track page + kit)

- Track page (joinbuilders.org/buildathon/tracks/builders), not the kit: practice = 48 matches against 15 house bots
  (4 easy, 4 medium, 4 hard, 3 expert) "acting as one squad: they share vision, never fight each other, and hunt you
  weakest first (one early, more as you thin them out, all of them late)". "Weak" is not defined. Our reading (guess):
  the squad sends its weakest bots at us first. So in practice our place = 16 - house bots dead before us, and they
  die only from our damage or (if at all) the zone: each kill is +1 place.
- Leaderboard top is now about 6.06 (5.94-6.06 in the top 5). The "Top 32 cutoff 5.23" is NOT confirmed: the page
  shows no cutoff.
- Practice seeds are private and fixed all event (two runs of sub-05/sub-05b both gave 2.31).
- The qualifier (top 32, 16 matches against other qualifiers) and the final (top 16, 64 matches, expert house bots
  fill empty slots) use NEW seeds. Gains that depend on the house-bot squad may not carry over; HP, weapons and
  survival gains are more likely to.
- Kills do not hand us cubes: a killed mech DROPS its cubes + 2 on the ground (protocol 10), and our bot never walks
  to cubes, so our upgrade code almost never runs.
- The recorded match (check/stream.jsonl.gz) is probably NOT a practice match: 3 mechs died at 18-27 s while we only
  ever saw mech 10, which a non-fighting squad cannot cause. Conclusions drawn from it (the sub-03 LOS-break case and
  the sub-07 first-shot timing) are weaker than they looked.

## Remaining plan (until Sunday ~1:00 PM UK)
Base = best-scoring tag so far: sub-09 = 2.48. One change per submission.
1. sub-08: ground weapon pickup radius 80 m -> 100 m: 2.27, no effect, dropped (radius stays 80 m).
2. sub-09: "get cubes" (cubes + crates within 60 m): 2.48, kept, new base.
   sub-10: cube and crate radius 60 m -> 80 m: 2.44, no effect, dropped (radius stays 60 m).
3. sub-06: rooftop weapons by jump + jets, on sub-09: built 21 Nov, submitted once; base stays sub-09 unless
   sub-06 scores >= 2.58.
4. sub-11: upgrade choice order, on sub-09 (approved 21 Nov; bot.py and loot.py restored byte-identical to sub-09 first).
5. Laser rule follow-up (only if a laser rule is retried): require the line to stay clear for 2-3 ticks before
   starting a charge, so blocked lines do not waste auto-fired shots (seen in the sub-07 recorded-match check).
Stop building about 1:00 PM UK Sunday; final checks after that.

## Offline test results

| Commit | tools/stress.py | check.py local | check.py container (sandbox flags) |
| --- | --- | --- | --- |
| sub-01 (step 0+1) | PASS: replay p99 6.2 ms, fuzz max 7.4 ms, routes 120/120 | PASS p99 6.4 ms | PASS p99 2.0 ms, max 2.1 ms |
| sub-02 (hide) | PASS: replay p99 6.2 ms, fuzz p99 6.3 / max 7.1 ms, routes 114/120 to the spot (misses: walker stuck in pockets it cannot jet out of) | PASS p99 6.4 ms | PASS p99 1.3 ms, max 1.4 ms (twice) |
| sub-03 (break LOS) | PASS on seeds 1, 2, 4: fuzz p99 8.0-8.2 ms, max 8.6-9.5 ms; replay p99 6.2 ms. Seed 3 routes 36/40, identical to sub-02's own code on that seed. Fault injection: 300/300 answered | PASS p99 6.4 ms | PASS p99 1.2-1.5 ms, max 1.8 ms (twice, built from 67ce118) |
| sub-04 (loot, on sub-01) | Seeds 1-3 PASS: fuzz p99 6.5-9.0 ms, max 7.2-14.1 ms (the slow ticks are sub-01's own new-circle setup; loot code max 0.09 ms); replay p99 6.2-6.4 ms. Default seed: routes 56/60 = FAIL at the 95% bar, identical for the restored sub-01 bot with the same test file (walker pockets). Injected loot crash: 300/300 answered with sub-01 moves | PASS p99 6.5 ms | PASS p99 1.1-2.0 ms, max 2.1 ms (twice, built from 25e5e53) |
| sub-05 (loot 80 m) | Seeds 1-3 PASS: fuzz p99 6.3-6.6 ms, max 7.4-13.2 ms; replay p99 6.2-6.4 ms. Default seed routes 56/60, same as sub-04 and the sub-01 bot. Injected loot crash: 300/300 answered | PASS p99 6.4 ms | PASS p99 1.2-1.9 ms, max 2.1 ms (twice, built from bd9167c) |
| sub-05b (loot fixes) | Seeds 1-3 PASS: fuzz p99 6.3 ms, max 6.8-10.0 ms; replay p99 6.2 ms. Default seed routes 56/60 (same as sub-05). Injected crash in loot update + read_events: 300/300 answered | PASS p99 6.5 ms | PASS p99 1.2-1.5 ms, max 1.8 ms (twice, built from 7b2029d) |
| sub-07 (laser long shots) | Seeds 1-3 PASS: fuzz p99 6.3 ms, max 6.7-8.8 ms; replay p99 6.1-6.2 ms; lone-mech scenario p99 0.13 ms, max 6.3 ms. Default seed routes 56/60 (same as sub-05b). Side by side vs sub-05b, no laser: 0/3000 random ticks differ; recorded 300 ticks 0 differ with the route field pre-finished (1 tick differs on 1 run in 3 for sub-05b vs itself too: wall-clock field slicing). Injected crash in the new rule: 300/300 answered | PASS p99 6.4 ms | PASS p99 1.0-1.8 ms, max 2.1 ms (twice, built from 30afa6b) |
| sub-09 (get cubes) | Seeds 1-3 PASS: fuzz p99 6.3 ms, max 6.6-7.1 ms; replay p99 6.2 ms. Default seed routes 56/60 (same as sub-05b). Crate path active (2000 ticks, 4 crates in range): p99 6.3 ms, max 6.6 ms. Side by side vs sub-05b with no crate/cube: 0/3000 random, 0/299 recorded (also 0/299 with the recorded crates/cubes kept: nearest crate 79 m). Injected crash in cube + crate code: 300/300 answered, 300/300 equal to sub-05b. Decide crash with an offer: fallback picks plating (sub-05b: no pick) | PASS p99 6.4 ms | PASS p99 1.3-1.4 ms, max 1.6 ms (twice, built from ef5de74) |
| sub-10 (cube/crate 80 m) | Seeds 1-3 PASS: fuzz p99 6.3 ms, max 6.6-7.5 ms; replay p99 6.2 ms. Default seed routes 56/60 (same as sub-09). Fuzz with crates/cubes out to 100 m: p99 6.3 ms, max 9.0 ms. Side by side vs sub-09 with nothing at 60-80 m: 0/3000 random ticks (9600 crates/cubes at 0-60 and 80-100 m kept), 0/299 recorded. Injected crash in cube + crate code: 300/300 answered. Synthetic 70 m on open ground: cube collected at 5.6 s, plating picked, back on route at 5.8 s; crate destroyed at 1.6 s (7 shots, walking toward it), early reload, cube at 5.6 s, plating, route at 5.8 s; sub-09 ignores both | PASS p99 6.5 ms | result lost with the old session (never written here) |
| sub-06 (rooftop weapons, on sub-09) | Seeds 2-3 PASS: fuzz p99 7.4-8.5 ms, max 11.2-14.7 ms (machine noise: the rule itself max 0.07 ms in fuzz, 0.26 ms with real roof planning; sub-09 on the same machine max 6.5-8.5 ms); replay p99 6.2-6.4 ms. Seed 1 routes 56/60, same as sub-09 on this machine (58/60, 60/60 on seeds 2-3, identical). Side by side vs sub-09 (`tools/sidebyside.py`, seeds 1 and 2): recorded 300, fuzz without roof pickups 3000, fuzz with them 3000: 0 differing actions each (the fuzz never triggers the rule); injected crash in RoofLooter.update: 300/300 and 3000/3000 answered, 0 differ. Closed-loop tests in `tools/jetsim.py` (sweep, recorded, gates): see the sub-06 section | PASS p99 6.4 ms, max 6.6 ms | PASS p99 1.6-2.1 ms (twice, working tree); PASS p99 3.4-3.9 ms, max 5.0 and 18.8 ms (twice, built from a git archive of 9c827c71c8ad1a0e5dcf84a185cb617a8ce617d1) |
| sub-08 (loot 100 m) | Seeds 1-3 PASS: fuzz p99 6.3 ms, max 6.6-9.3 ms; replay p99 6.2 ms. Default seed routes 56/60 (same as sub-05b). Side by side vs sub-05b with pickups at 80-100 m removed: 0/3000 random ticks, 0/299 recorded ticks (route field pre-finished). Synthetic laser 90 m away on open ground: sub-08 takes it at t=7.4 s and resumes the route; sub-05b ignores it. Injected loot crash: 300/300 answered | PASS p99 6.4 ms | PASS p99 1.2-1.4 ms (4 runs, built from da09fcf) |

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
| sub-08 | da09fcf24932dc4b5b5e634ce53bf37d2218dbe3 | sub-05b with ground pickup radius 100 m | 2.27 (no effect vs 2.31: drop 100 m, do not build on it) |
| sub-09 | ef5de74c8756f219470170cbabc3f3a217b57a69 | sub-05b + get cubes (cube pickup + crate shooting within 60 m, upgrade pick in the crash fallback) | 2.48 (+0.17 vs 2.31: keep, base for sub-10) |
| sub-10 | be98e65d4328e12fde3ab1eeedcbb6e24335393b | sub-09 with cube and crate radius 80 m | 2.44 (no effect vs 2.48: drop 80 m, cube/crate radius stays 60 m) |
| sub-06 | 9c827c71c8ad1a0e5dcf84a185cb617a8ce617d1 | sub-09 + rooftop laser/lightning by jump + jets | pending (expected no readable signal: fires in ~3-6% of matches) |

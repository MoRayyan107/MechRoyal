"""Side-by-side: run two bot versions on the same observation streams and compare every action.

  python tools/sidebyside.py [--base sub-09] [--seed 1] [--fuzz 3000]                    (sub-06 roof rule)
  python tools/sidebyside.py --mode upgrades [--base sub-09] [--seed 1] [--fuzz 3000]   (sub-11 upgrade order)

The base version's bot files are exported from its git tag into a scratch folder; each bot runs in its own
process (so the two `bot` / `loot` modules never mix). Both get a fake clock (0.1 ms per call) so the route field is sliced
identically and wall-clock timing cannot make the two differ; the reported ms use the real clock.

Streams:
  recorded        the kit's 300 recorded observations (no rooftop candidate qualifies: rule off)
  fuzz-noroof     stress.py's fuzz observations with every pickup that lies on a building cell removed
  fuzz            the same fuzz observations, rooftop pickups kept (the rule may fire: diffs are expected, and
                  every diff must come on or after a tick where the new rule was active)
  recorded-crash  recorded stream, new bot with RoofLooter.update raising every call (must equal the base)
  fuzz-crash      fuzz stream (rooftop pickups kept), new bot with RoofLooter.update raising every call

--mode upgrades (only the upgrade pick may differ, and only on ticks with an offer pending):
  recorded / fuzz  every tick without an offer must match exactly; on offer ticks only `pick` may differ
  offers           the last recorded observation with every possible 3-choice offer (gun only; gun + laser;
                   gun + lightning), run through act() of both bots: gives the table of picks where they differ
  *-crash          Bot.decide raising every call in BOTH versions (the crash fallback answers): same checks
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ("bot.py", "loot.py", "nav.py", "mechfight.py")


def runner(botdir, hello_path, obs_path, out_path, inject, inject_decide=False):
    sys.path.insert(0, botdir)
    real_clock = time.perf_counter
    import bot as botmod
    import loot
    import nav

    # deterministic slicing: a fake clock that advances 0.1 ms per call, so the route field is sliced the same way
    # in both versions whenever they run the same code (timings below use the real clock)
    class FakeTime:
        t = 0.0

        @classmethod
        def perf_counter(cls):
            cls.t += 0.0001
            return cls.t

    botmod.time = FakeTime
    nav.time = FakeTime
    time_perf = real_clock
    if inject:
        def boom(self, *a, **k):
            raise RuntimeError("injected roof crash")
        loot.RoofLooter.update = boom
    if inject_decide:
        def boom_decide(self, *a, **k):
            raise RuntimeError("injected decide crash")
        botmod.Bot.decide = boom_decide
    with open(hello_path, encoding="utf-8") as fh:
        hello = json.load(fh)
    with contextlib.redirect_stderr(io.StringIO()):
        b = botmod.Bot(hello)
    out = []
    with open(obs_path, encoding="utf-8") as fh:
        for line in fh:
            o = json.loads(line)
            t = time_perf()
            e0 = b.errors
            with contextlib.redirect_stderr(io.StringIO()) as err:
                a = b.act(o)
            dt = (time_perf() - t) * 1000
            if b.errors > e0:
                err.write("decide failed")           # the bot logs only its first 5 failures: count them all
            ro = getattr(b, "roofer", None)
            active = ro is not None and (ro.cur is not None or ro.landed is not None)
            out.append({"a": a, "ms": dt, "roof": active, "fell_back": "decide failed" in err.getvalue(),
                        "offer": bool(o.get("offer"))})
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh)


def export(tag, dest):
    os.makedirs(dest, exist_ok=True)
    for f in FILES:
        data = subprocess.run(["git", "show", f"{tag}:{f}"], cwd=ROOT, capture_output=True, check=True).stdout
        with open(os.path.join(dest, f), "wb") as fh:
            fh.write(data)


def run_bot(botdir, hello_path, obs_path, inject=False, inject_decide=False):
    out_path = (obs_path + (".inj" if inject else "") + (".injd" if inject_decide else "") + "."
                + os.path.basename(botdir) + ".json")
    cmd = [sys.executable, os.path.abspath(__file__), "--run", botdir, hello_path, obs_path, out_path]
    if inject:
        cmd.append("--inject")
    if inject_decide:
        cmd.append("--inject-decide")
    subprocess.run(cmd, check=True)
    with open(out_path, encoding="utf-8") as fh:
        return json.load(fh)


def compare(name, base, new):
    n = len(base)
    diffs = [i for i in range(n) if json.dumps(base[i]["a"], sort_keys=True) != json.dumps(new[i]["a"], sort_keys=True)]
    first_roof = next((i for i in range(n) if new[i]["roof"]), None)
    before = [i for i in diffs if first_roof is None or i < first_roof]
    answered = sum(1 for r in new if r["a"].get("tick") is not None)
    fb = sum(1 for r in new if r["fell_back"])
    ms = sorted(r["ms"] for r in new)
    p99 = ms[min(n - 1, int(0.99 * n))]
    roof_ticks = sum(1 for r in new if r["roof"])
    print(f"{name:15s} {n} ticks: {len(diffs)} differ from base; {len(before)} differ before the new rule was first"
          f" active (first active tick: {first_roof}); rule active on {roof_ticks} ticks; answered {answered}/{n};"
          f" decide fallbacks {fb}; new ms p99 {p99:.2f} max {ms[-1]:.2f}")
    return diffs, before


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--run":
        runner(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], "--inject" in sys.argv,
               "--inject-decide" in sys.argv)
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="sub-09")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--fuzz", type=int, default=3000)
    ap.add_argument("--tmp", default=None)
    ap.add_argument("--mode", choices=["roof", "upgrades"], default="roof")
    args = ap.parse_args()
    tmp = args.tmp or tempfile.mkdtemp(prefix="sbs_")
    base_dir = os.path.join(tmp, "base")
    new_dir = os.path.join(tmp, "new")
    export(args.base, base_dir)
    os.makedirs(new_dir, exist_ok=True)
    for f in FILES:
        with open(os.path.join(ROOT, f), "rb") as src, open(os.path.join(new_dir, f), "wb") as dst:
            dst.write(src.read())

    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import stress
    import nav
    hello, stream = stress.load_stream()
    hello_path = os.path.join(tmp, "hello.json")
    with open(hello_path, "w", encoding="utf-8") as fh:
        json.dump(hello, fh)
    m = hello["map"]
    with contextlib.redirect_stderr(io.StringIO()):
        grid = nav.Grid(m["dir"], m["files"], hello["config"]["mech"])
    rng = random.Random(args.seed)
    fuzz = [stress.fake_obs(rng, grid, 2000 + k * 2, stream[-1]) for k in range(args.fuzz)]

    def on_roof_cell(p):
        c = grid.cell_of(p["pos"][0], p["pos"][1])
        return c >= 0 and grid.blocked[c]

    if args.mode == "upgrades":
        sys.exit(upgrades_mode(args, tmp, base_dir, new_dir, hello, hello_path, stream, fuzz))

    noroof = []
    for o in fuzz:
        o2 = json.loads(json.dumps(o))
        o2["pickups"] = [p for p in o2.get("pickups") or [] if not on_roof_cell(p)]
        noroof.append(o2)
    streams = {"recorded": stream, "fuzz-noroof": noroof, "fuzz": fuzz}
    paths = {}
    for k, v in streams.items():
        paths[k] = os.path.join(tmp, k + ".jsonl")
        with open(paths[k], "w", encoding="utf-8") as fh:
            for o in v:
                fh.write(json.dumps(o) + "\n")
    print(f"base = {args.base} (exported to {base_dir}); new = working tree; fuzz seed {args.seed}; scratch {tmp}")
    ok = True
    for k in ("recorded", "fuzz-noroof", "fuzz"):
        b = run_bot(base_dir, hello_path, paths[k])
        n = run_bot(new_dir, hello_path, paths[k])
        diffs, before = compare(k, b, n)
        if k != "fuzz" and diffs:
            ok = False
        if before:
            ok = False
        if k == "fuzz":
            for k2 in ("recorded", "fuzz"):
                bb = b if k2 == "fuzz" else run_bot(base_dir, hello_path, paths[k2])
                ni = run_bot(new_dir, hello_path, paths[k2], inject=True)
                d2, _ = compare(k2 + "-crash", bb, ni)
                if d2 or any(r["a"].get("tick") is None for r in ni):
                    ok = False
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def compare_upgrades(name, base, new):
    """Ticks without an offer: must match exactly. Offer ticks: only `pick` may differ."""
    n = len(base)
    no_offer_diff = []
    offer_ticks = 0
    pick_diff = 0
    other_diff_on_offer = []
    for i in range(n):
        a, b = base[i]["a"], new[i]["a"]
        same = json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        if not new[i]["offer"]:
            if not same:
                no_offer_diff.append(i)
            continue
        offer_ticks += 1
        if a.get("pick") != b.get("pick"):
            pick_diff += 1
        a2 = {k: v for k, v in a.items() if k != "pick"}
        b2 = {k: v for k, v in b.items() if k != "pick"}
        if json.dumps(a2, sort_keys=True) != json.dumps(b2, sort_keys=True):
            other_diff_on_offer.append(i)
    answered = sum(1 for i in range(n) if new[i]["a"].get("tick") is not None
                   and new[i]["a"].get("tick") == base[i]["a"].get("tick"))
    fb = sum(1 for r in new if r["fell_back"])
    print(f"{name:15s} {n} ticks ({n - offer_ticks} without an offer): {len(no_offer_diff)} differ without an offer;"
          f" offer ticks {offer_ticks}: pick differs on {pick_diff}, anything else differs on"
          f" {len(other_diff_on_offer)}; answered {answered}/{n}; decide fallbacks {fb}")
    return not no_offer_diff and not other_diff_on_offer and answered == n


def upgrades_mode(args, tmp, base_dir, new_dir, hello, hello_path, stream, fuzz):
    import itertools
    ids = [u["id"] for u in hello["config"]["upgrades"]]
    req = {u["id"]: u.get("requires") for u in hello["config"]["upgrades"]}
    names = {u["id"]: (u.get("name", ""), u.get("desc", "")) for u in hello["config"]["upgrades"]}
    pools = {}
    for label, held in (("gun only", {"gun"}), ("gun + laser", {"gun", "laser"}),
                        ("gun + lightning", {"gun", "lightning"})):
        pools[label] = [i for i in ids if req[i] is None or req[i] in held]
    offers = []
    tick = 100000
    for label, pool in pools.items():
        for combo in itertools.combinations(pool, 3):
            o = json.loads(json.dumps(stream[-1]))
            tick += 2
            o["tick"] = tick
            o["t"] = tick / 20.0
            o["offer"] = {"id": tick, "expires": o["t"] + 5,
                          "choices": [{"id": c, "name": names[c][0], "desc": names[c][1]} for c in combo]}
            o["_pool"] = label
            offers.append(o)
    streams = {"recorded": stream, "fuzz": fuzz, "offers": offers}
    paths = {}
    for k, v in streams.items():
        paths[k] = os.path.join(tmp, "u_" + k + ".jsonl")
        with open(paths[k], "w", encoding="utf-8") as fh:
            for o in v:
                fh.write(json.dumps(o) + "\n")
    print(f"mode upgrades: base = {args.base} (exported to {base_dir}); new = working tree; fuzz seed {args.seed}")
    ok = True
    results = {}
    for k in ("recorded", "fuzz", "offers"):
        b = run_bot(base_dir, hello_path, paths[k])
        n = run_bot(new_dir, hello_path, paths[k])
        results[k] = (b, n)
        ok = compare_upgrades(k, b, n) and ok
    for k in ("recorded", "fuzz", "offers"):
        b = run_bot(base_dir, hello_path, paths[k], inject_decide=True)
        n = run_bot(new_dir, hello_path, paths[k], inject_decide=True)
        ok = compare_upgrades(k + "-crash", b, n) and ok
        if not all(r["fell_back"] for r in n):
            print(f"  ({k}-crash: not every tick fell back: the injection did not take)")
            ok = False
    # table of picks where the two orders differ
    b, n = results["offers"]
    for label in pools:
        idx = [i for i, o in enumerate(offers) if o["_pool"] == label]
        table = {}
        new_counts = {}
        base_counts = {}
        for i in idx:
            ch = [c["id"] for c in offers[i]["offer"]["choices"]]
            pb, pn = ch[b[i]["a"]["pick"]], ch[n[i]["a"]["pick"]]
            base_counts[pb] = base_counts.get(pb, 0) + 1
            new_counts[pn] = new_counts.get(pn, 0) + 1
            if pb != pn:
                table[(pb, pn)] = table.get((pb, pn), 0) + 1
        d = sum(table.values())
        print(f"\noffers, {label}: {len(idx)} possible offers; the pick differs on {d} ({d / len(idx):.0%})")
        print("  base picks: " + ", ".join(f"{k} {v / len(idx):.0%}"
                                          for k, v in sorted(base_counts.items(), key=lambda kv: -kv[1])))
        print("  new picks:  " + ", ".join(f"{k} {v / len(idx):.0%}"
                                          for k, v in sorted(new_counts.items(), key=lambda kv: -kv[1])))
        print("  where they differ (base -> new: count, share of all offers):")
        for (pb, pn), c in sorted(table.items(), key=lambda kv: -kv[1]):
            print(f"    {pb:>20s} -> {pn:<20s} {c:4d}  {c / len(idx):5.1%}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    main()

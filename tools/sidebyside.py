"""Side-by-side: run two bot versions on the same observation streams and compare every action.

  python tools/sidebyside.py [--base sub-09] [--seed 1] [--fuzz 3000]

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


def runner(botdir, hello_path, obs_path, out_path, inject):
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
    with open(hello_path, encoding="utf-8") as fh:
        hello = json.load(fh)
    with contextlib.redirect_stderr(io.StringIO()):
        b = botmod.Bot(hello)
    out = []
    with open(obs_path, encoding="utf-8") as fh:
        for line in fh:
            o = json.loads(line)
            t = time_perf()
            with contextlib.redirect_stderr(io.StringIO()) as err:
                a = b.act(o)
            dt = (time_perf() - t) * 1000
            ro = getattr(b, "roofer", None)
            active = ro is not None and (ro.cur is not None or ro.landed is not None)
            out.append({"a": a, "ms": dt, "roof": active, "fell_back": "decide failed" in err.getvalue()})
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh)


def export(tag, dest):
    os.makedirs(dest, exist_ok=True)
    for f in FILES:
        data = subprocess.run(["git", "show", f"{tag}:{f}"], cwd=ROOT, capture_output=True, check=True).stdout
        with open(os.path.join(dest, f), "wb") as fh:
            fh.write(data)


def run_bot(botdir, hello_path, obs_path, inject=False):
    out_path = obs_path + (".inj" if inject else "") + "." + os.path.basename(botdir) + ".json"
    cmd = [sys.executable, os.path.abspath(__file__), "--run", botdir, hello_path, obs_path, out_path]
    if inject:
        cmd.append("--inject")
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
        runner(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], "--inject" in sys.argv)
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="sub-09")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--fuzz", type=int, default=3000)
    ap.add_argument("--tmp", default=None)
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


if __name__ == "__main__":
    main()

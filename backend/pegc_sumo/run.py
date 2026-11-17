"""Run a scenario in SUMO and print the measured metrics.

    python -m pegc_sumo.run --strategy A --seed 7 --density medium
"""
from __future__ import annotations

import argparse
import json
import os

from .config import Scenario
from .runner import run_scenario

ROWS = [("ambTravelTime", "Ambulance travel time (s)"), ("ambStops", "Ambulance stops"),
        ("avgWait", "Average wait, other vehicles (s)"), ("longestWait", "Longest single wait (s)"),
        ("maxQueue", "Largest queue (vehicles)"), ("throughput", "Throughput (vehicles per hour)"),
        ("recoveryTime", "Recovery time (s)"), ("recoveryBaseline", "Pre-ambulance queue average"),
        ("completed", "Vehicles completed"), ("violations", "Safety violations")]


def _fmt(v):
    return "n/a" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))


def js_reference(strategy: str, density: str, seed: int):
    """Browser-engine result for the same strategy, density and seed, if it is in frontend/baseline.json."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(here, "..", "..", "frontend", "baseline.json"))
    if not os.path.exists(path):
        return None
    return json.load(open(path, encoding="utf-8")).get(f"{strategy}|{density}|{seed}", {}).get("metrics")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="A", choices=["A", "B", "C", "C1", "C2"])
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--density", default="medium", choices=["low", "medium", "high"])
    ap.add_argument("--origin", default="W0")
    ap.add_argument("--dest", default="E1")
    ap.add_argument("--block", action="append", default=[], help='blocked segment such as "J00|J01" (repeatable)')
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    a = ap.parse_args()
    s = Scenario(origin=a.origin, dest=a.dest, density=a.density, seed=a.seed, blocked=tuple(a.block), strategy=a.strategy)
    r = run_scenario(s, a.strategy)
    if a.json:
        print(json.dumps({"metrics": r.metrics, "safety": r.safety, "log": r.log, "series": r.series}, indent=1))
        return
    ref = js_reference(a.strategy, a.density, a.seed)
    print(f"Strategy {a.strategy} | {a.density} traffic | seed {a.seed} | {a.origin} -> {a.dest} | SUMO\n")
    print(f"{'Metric':40s}{'SUMO':>12s}" + (f"{'Browser engine':>18s}" if ref else ""))
    for k, label in ROWS:
        line = f"{label:40s}{_fmt(r.metrics.get(k)):>12s}"
        if ref:
            line += f"{_fmt(ref.get(k)):>18s}"
        print(line)
    print(f"\nSignal switches: {r.switches} | safety byRule: {r.safety['byRule']} | wall time: {r.wall_seconds} s")
    for e in r.log:
        print(f"  [{e['t']:7.2f} s] {e['text']}")
    if ref:
        print("\n(The browser engine and SUMO are different simulators, so the numbers are of the same order but not identical.)")


if __name__ == "__main__":
    main()

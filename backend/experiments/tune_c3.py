"""Step 5b: tune Strategy C3's cross-wait weight on the TUNING seeds (1-5) only. Test seeds are never touched here.

    python -m experiments.tune_c3 [--weights 1,2,4,8] [--workers 4]

Runs A, C and C3 at each candidate weight on the same 60 tuning scenarios (3 densities x 2 routes x open/blocked x
seeds 1-5), saves every run to c3_tuning_runs.csv, the per-weight table and the decision to c3_tuning.json.

Selection rule (fixed BEFORE looking at any result): over all tuning runs pooled, a weight is feasible when its mean
longest single wait is <= Strategy A's. Among feasible weights pick the lowest mean ambulance time (finished runs).
If none is feasible, pick the lowest mean longest wait and record "constraint_not_met". C3 at 0.5 would equal C.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .analyze import mean_std
from .batch import DEFAULT_CALIBRATION, HERE, build_jobs, run_jobs, write_csv, _run_job

WEIGHTS = (1.0, 2.0, 4.0, 8.0)


def _pool(rows: list, key: str, finished_only: bool = False) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None and (not finished_only or r["amb_finished"])]
    return mean_std(vals)[0]


def table(rows: list, weights: tuple) -> list:
    groups = [("A", None, "A"), ("C", None, "C")] + [("C3", w, f"C3 w={w:g}") for w in weights]
    out = []
    for strat, w, name in groups:
        sel = [r for r in rows if r["strategy"] == strat and (w is None or r["c3_weight"] == w)]
        out.append({"name": name, "strategy": strat, "weight": w, "runs": len(sel),
                    "amb_finished": sum(1 for r in sel if r["amb_finished"]),
                    "amb_time": _pool(sel, "amb_time", True), "avg_wait": _pool(sel, "avg_wait"),
                    "longest_wait": _pool(sel, "longest_wait"), "wait_p95": _pool(sel, "wait_p95"),
                    "max_queue": _pool(sel, "max_queue"), "q120_after": _pool(sel, "q120_after")})
    return out


def choose(tab: list) -> dict:
    a = next(t for t in tab if t["strategy"] == "A")
    cands = [t for t in tab if t["strategy"] == "C3"]
    feasible = [t for t in cands if t["longest_wait"] is not None and t["longest_wait"] <= a["longest_wait"]]
    if feasible:
        best, status = min(feasible, key=lambda t: float("inf") if t["amb_time"] is None else t["amb_time"]), "ok"
    else:
        best, status = min(cands, key=lambda t: t["longest_wait"]), "constraint_not_met"
    return {"weight": best["weight"], "status": status}


def main(argv=None, run_job=None) -> int:
    p = argparse.ArgumentParser(description="Tune C3 on tuning seeds only")
    p.add_argument("--weights", default=",".join(f"{w:g}" for w in WEIGHTS))
    p.add_argument("--workers", type=int, default=min(os.cpu_count() or 1, 4))
    p.add_argument("--calibration", default=DEFAULT_CALIBRATION)
    p.add_argument("--outdir", default=HERE)
    args = p.parse_args(argv)
    weights = tuple(float(x) for x in args.weights.split(","))
    scale = json.load(open(args.calibration, encoding="utf-8"))["scale"]
    if scale is None:
        print("calibration has no scale", file=sys.stderr)
        return 2
    entries = ("A", "C") + tuple(f"C3@{w!r}" for w in weights)
    jobs = build_jobs(scale, strategies=entries, splits=("tune",))
    print(f"{len(jobs)} tuning scenarios x {len(entries)} = {len(jobs) * len(entries)} runs (seeds 1-5 only)", file=sys.stderr)
    if run_job is None:
        from pegc_sumo.sumo_io import build_static_network

        build_static_network()
        run_job = _run_job
    rows, errors = run_jobs(jobs, run_job, max(1, args.workers))
    if errors:
        for jid, tb in sorted(errors.items()):
            print(f"JOB {jid} FAILED:\n{tb}", file=sys.stderr)
        return 1
    write_csv(rows, os.path.join(args.outdir, "c3_tuning_runs.csv"))
    tab = table(rows, weights)
    choice = choose(tab)
    with open(os.path.join(args.outdir, "c3_tuning.json"), "w", encoding="utf-8") as f:
        json.dump({"split": "tune", "seeds": [1, 2, 3, 4, 5], "scale": scale, "rule": __doc__.split("Selection rule")[1].strip(),
                   "table": tab, "choice": choice}, f, indent=1)
        f.write("\n")
    print(f"{'':<12}{'runs':>5}{'amb fin':>8}{'amb time':>10}{'avg wait':>10}{'longest':>9}{'p95 wait':>10}{'max q':>7}{'q+120s':>8}")
    f1 = lambda v: "n/a" if v is None else f"{v:.1f}"
    for t in tab:
        print(f"{t['name']:<12}{t['runs']:>5}{t['amb_finished']:>8}{f1(t['amb_time']):>10}{f1(t['avg_wait']):>10}"
              f"{f1(t['longest_wait']):>9}{f1(t['wait_p95']):>10}{f1(t['max_queue']):>7}{f1(t['q120_after']):>8}")
    print(f"\nChosen C3 weight: {choice['weight']:g} (status: {choice['status']})")
    print(f"Final test run, ONCE:  python -m experiments.batch --split test --strategies A,B,C,C1,C2,C3 "
          f"--c3-weight {choice['weight']:g} --out experiments/results_5b.csv --workers 4")
    return 0


if __name__ == "__main__":
    sys.exit(main())

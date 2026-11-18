"""Step 5, part 1: calibrate the SUMO traffic presets, then record how in calibration.json.

Why: in SUMO the browser's "medium" demand is oversaturated (the total queue keeps growing during the run, see
HANDOVER fact 1). Strategy comparisons are meaningless in a network that is jamming up regardless of the signals.

Method (everything is measured, nothing is assumed):
  * Strategy A (fixed time) runs the default route on CALIBRATION seeds 201-205 (separate from tuning seeds 1-5
    and test seeds 101-110) with the browser's medium rate multiplied by each scale of a ladder.
  * growth = mean total queue in the last 60 s (360-420 s) minus mean total queue in 180-240 s. A network that is
    busy but stable has growth near 0; an oversaturated one keeps growing.
  * a scale passes if the MEAN growth over the seeds is <= --growth-max (a design choice, recorded in the file).
  * the chosen scale is the largest one for which it and every smaller scale pass.
  * low, medium and high keep the browser's ratio (180 : 330 : 520); all three are multiplied by the chosen scale.
    The same rates are then used for every strategy. Rates are clamped to the runner's range (100 to 900 veh/h).
  * the saturation of the three calibrated presets is measured and recorded too; "high" may still be saturated,
    and the file says so instead of hiding it.

    python -m experiments.calibrate
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from pegc_sumo.config import CONFIG, Scenario

from .batch import DEFAULT_CALIBRATION, DENSITIES, rate_for, run_jobs

CAL_SEEDS = (201, 202, 203, 204, 205)
LADDER = tuple(round(0.40 + 0.05 * i, 2) for i in range(13))     # 0.40 ... 1.00
GROWTH_MAX = 6.0
EARLY = (180.0, 240.0)
LATE = (360.0, 420.0)
ROUTE = ("W0", "E1")


def window_mean(series: list, lo: float, hi: float) -> float:
    vals = [q for t, q in series if lo < t <= hi]
    if not vals:
        raise ValueError(f"no queue samples in ({lo}, {hi}]")
    return sum(vals) / len(vals)


def queue_growth(series: list) -> float:
    """Mean total queue late in the run minus mean total queue in the middle of the run."""
    return window_mean(series, *LATE) - window_mean(series, *EARLY)


def choose_scale(ladder: list, growth_max: float):
    """ladder: [(scale, mean growth)]. Largest scale for which it and all smaller scales pass; None if even the smallest fails."""
    best = None
    for scale, growth in sorted(ladder):
        if growth > growth_max:
            break
        best = scale
    return best


def _calib_job(job: dict) -> list:
    from pegc_sumo.runner import run_scenario

    s = Scenario(origin=ROUTE[0], dest=ROUTE[1], density="custom", rate=float(job["rate"]), seed=job["seed"])
    r = run_scenario(s, "A", label=f"c{os.getpid()}_{job['id']}")
    early, late = window_mean(r.series, *EARLY), window_mean(r.series, *LATE)
    m = r.metrics
    return [{"scale": job["scale"], "density": job["density"], "rate": job["rate"], "seed": job["seed"],
             "growth": late - early, "early_queue": early, "late_queue": late, "max_queue": m["maxQueue"],
             "avg_wait": m["avgWait"], "throughput": m["throughput"], "entered": m["entered"],
             "completed": m["completed"], "violations": m["violations"]}]


def _mean(rows: list, key: str) -> float:
    return sum(r[key] for r in rows) / len(rows)


def summarize(rows: list, growth_max: float) -> dict:
    out = {"mean_growth": _mean(rows, "growth"), "max_growth": max(r["growth"] for r in rows),
           "mean_max_queue": _mean(rows, "max_queue"), "mean_avg_wait": _mean(rows, "avg_wait"),
           "mean_throughput": _mean(rows, "throughput"), "mean_entered": _mean(rows, "entered"),
           "mean_completed": _mean(rows, "completed"), "seeds": [r["seed"] for r in rows],
           "growth_per_seed": [r["growth"] for r in rows]}
    out["passes"] = out["mean_growth"] <= growth_max
    return out


def sumo_version() -> str:
    try:
        from pegc_sumo.sumo_io import find_binary

        out = subprocess.run([find_binary("sumo"), "--version"], capture_output=True, text=True, timeout=30).stdout
        return out.strip().splitlines()[0]
    except Exception:
        return "unknown"


def main(argv=None, run_job=None) -> int:
    p = argparse.ArgumentParser(description="Calibrate SUMO traffic presets -> calibration.json")
    p.add_argument("--out", default=DEFAULT_CALIBRATION)
    p.add_argument("--growth-max", type=float, default=GROWTH_MAX)
    p.add_argument("--workers", type=int, default=min(os.cpu_count() or 1, 4))
    p.add_argument("--ladder", default=",".join(str(x) for x in LADDER), help="comma-separated scales to try")
    p.add_argument("--seeds", default=",".join(str(x) for x in CAL_SEEDS))
    args = p.parse_args(argv)
    ladder = tuple(sorted(float(x) for x in args.ladder.split(",")))
    seeds = tuple(int(x) for x in args.seeds.split(","))
    real = run_job is None
    if real:
        from pegc_sumo.sumo_io import build_static_network

        build_static_network()
        run_job = _calib_job

    def make_jobs(items):
        return [{"id": i, "scale": sc, "density": d, "rate": rate_for(d, sc), "seed": sd,
                 "label": f"calibration {d} scale {sc} rate {rate_for(d, sc)} seed {sd}"}
                for i, (sc, d, sd) in enumerate(items)]

    rows, errors = run_jobs(make_jobs([(sc, "medium", sd) for sc in ladder for sd in seeds]), run_job, max(1, args.workers))
    if errors:
        for jid, tb in sorted(errors.items()):
            print(f"JOB {jid} FAILED:\n{tb}", file=sys.stderr)
        return 1
    table = []
    for sc in ladder:
        res = summarize([r for r in rows if r["scale"] == sc], args.growth_max)
        table.append({"scale": sc, "rate": rate_for("medium", sc), **res})
    scale = choose_scale([(t["scale"], t["mean_growth"]) for t in table], args.growth_max)
    status = "no_scale_passed" if scale is None else ("all_passed" if all(t["passes"] for t in table) else "ok")

    presets, checks = {}, {}
    if scale is not None:
        extra = [d for d in DENSITIES if d != "medium"]
        rows2, errors = run_jobs(make_jobs([(scale, d, sd) for d in extra for sd in seeds]), run_job, max(1, args.workers))
        if errors:
            for jid, tb in sorted(errors.items()):
                print(f"JOB {jid} FAILED:\n{tb}", file=sys.stderr)
            return 1
        for d in DENSITIES:
            raw = CONFIG.density[d] * scale
            presets[d] = {"browser_rate": CONFIG.density[d], "rate": rate_for(d, scale), "clamped": round(raw) != rate_for(d, scale)}
            src = [r for r in rows if r["scale"] == scale] if d == "medium" else [r for r in rows2 if r["density"] == d]
            res = summarize(src, args.growth_max)
            checks[d] = {"mean_growth": res["mean_growth"], "mean_max_queue": res["mean_max_queue"],
                         "saturated": not res["passes"]}

    cal = {"version": 1, "sumo_version": sumo_version() if real else "not recorded (test run)",
           "method": "Strategy A, default route W0>E1, medium preset rate x scale, calibration seeds; growth = mean total "
                     "queue in 360-420 s minus mean total queue in 180-240 s; a scale passes when the mean growth over "
                     "the seeds <= growth_max; chosen scale = largest scale where it and every smaller scale pass.",
           "growth_max": args.growth_max, "windows": {"early": list(EARLY), "late": list(LATE)},
           "seeds": list(seeds), "route": list(ROUTE), "strategy": "A", "ladder": table, "scale": scale,
           "status": status, "presets": presets, "preset_checks": checks}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(cal, f, indent=1)
        f.write("\n")

    print(f"{'scale':>6} {'rate':>5} {'growth(mean)':>13} {'growth(max)':>12} {'max queue':>10} {'avg wait':>9} "
          f"{'veh/h':>7}  pass")
    for t in table:
        print(f"{t['scale']:>6.2f} {t['rate']:>5} {t['mean_growth']:>13.1f} {t['max_growth']:>12.1f} "
              f"{t['mean_max_queue']:>10.1f} {t['mean_avg_wait']:>9.1f} {t['mean_throughput']:>7.0f}  "
              f"{'yes' if t['passes'] else 'NO'}")
    if scale is None:
        print(f"\nNO scale passed (growth <= {args.growth_max}). Try a lower --ladder start, or raise --growth-max "
              f"and say so. Nothing usable was written for the batch.")
        print(f"wrote {args.out}")
        return 1
    print(f"\nChosen scale: {scale} (status: {status}); presets (veh/h per entry): "
          + ", ".join(f"{d} {presets[d]['rate']}{' (clamped)' if presets[d]['clamped'] else ''}" for d in DENSITIES))
    for d in DENSITIES:
        c = checks[d]
        print(f"  {d:<6} mean queue growth {c['mean_growth']:>6.1f}  {'STILL SATURATED' if c['saturated'] else 'not saturated'}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Step 5: batch experiments. Runs every strategy over densities, routes, blockage on/off and many seeds in SUMO
and writes one row per run to results.csv. All later numbers (analyze.py) come ONLY from that CSV.

    python -m experiments.batch --quick          # small smoke grid
    python -m experiments.batch --workers 4      # full grid

Fairness rules (all enforced here, not left to the caller):
  * every strategy sees the SAME scenario: same demand file, same SUMO seed, same ambulance route;
  * traffic rates are the browser presets multiplied by ONE calibrated scale (calibration.json, made by
    experiments/calibrate.py) and the scale is identical for every strategy;
  * seeds are split: tuning seeds 1-5 (for choosing weights) and test seeds 101-110 (held out);
  * one worker process owns one whole scenario (all strategies, one after the other): the routes file of a
    scenario is shared by its strategies, so two workers must never touch the same scenario;
  * rows are sorted into a fixed order, so re-running gives a byte-identical CSV.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import traceback

from pegc_sumo.config import CONFIG, Scenario, entry_rate
from pegc_sumo.network import build_network, find_route

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CALIBRATION = os.path.join(HERE, "calibration.json")
DEFAULT_RESULTS = os.path.join(HERE, "results.csv")

STRATEGIES = ("A", "B", "C", "C1", "C2")
DENSITIES = ("low", "medium", "high")
ROUTES = (("W0", "E1"), ("W1", "E0"))          # W0>E1 is the route used since Step 1; W1>E0 crosses other junctions
TUNE_SEEDS = (1, 2, 3, 4, 5)
TEST_SEEDS = tuple(range(101, 111))

COLUMNS = ["split", "strategy", "density", "rate", "scale", "origin", "dest", "blocked", "seed",
           "amb_finished", "amb_time", "amb_stops", "avg_wait", "longest_wait", "max_queue", "throughput",
           "completed", "entered", "recovery_time", "recovery_baseline", "violations", "switches", "routes_sha1"]
STR_COLS = {"split", "strategy", "density", "origin", "dest", "blocked", "routes_sha1"}
INT_COLS = {"rate", "seed", "amb_finished", "amb_stops", "completed", "entered", "violations", "switches"}
# CSV column -> key in RunResult.metrics
METRIC_KEYS = {"amb_finished": "ambFinished", "amb_time": "ambTravelTime", "amb_stops": "ambStops",
               "avg_wait": "avgWait", "longest_wait": "longestWait", "max_queue": "maxQueue",
               "throughput": "throughput", "completed": "completed", "entered": "entered",
               "recovery_time": "recoveryTime", "recovery_baseline": "recoveryBaseline", "violations": "violations"}


# ----------------------------------------------------------------------------------------------- grid
def rate_for(density: str, scale: float, cfg=CONFIG) -> int:
    """Entry rate (veh/h per entry) of a preset after calibration. Same rounding and clamping as the runner uses."""
    return entry_rate(Scenario(density="custom", rate=cfg.density[density] * scale), cfg)


def pick_blocked(net, origin: str, dest: str) -> str:
    """One inner road segment on the normal route whose closure still leaves a different route. Deterministic:
    the middle segment, then the others in route order."""
    route = find_route(net, origin, dest)
    inner = [e.seg_id for e in route
             if net.nodes[e.frm].type == "junction" and net.nodes[e.to].type == "junction"]
    normal = [e.id for e in route]
    mid = len(inner) // 2
    for seg in inner[mid:] + inner[:mid]:
        alt = find_route(net, origin, dest, frozenset({seg}))
        if alt is not None and [e.id for e in alt] != normal:
            return seg
    raise ValueError(f"no usable blocked segment for {origin}>{dest}")


def build_jobs(scale: float, strategies=STRATEGIES, densities=DENSITIES, routes=ROUTES, blocked_options=(False, True),
               tune_seeds=TUNE_SEEDS, test_seeds=TEST_SEEDS, splits=("tune", "test"), cfg=CONFIG) -> list:
    """One job = one scenario (run under every strategy). Order is fixed: split, density, route, blockage, seed."""
    net = build_network()
    seeds = {"tune": tune_seeds, "test": test_seeds}
    jobs = []
    for split in splits:
        for density in densities:
            for origin, dest in routes:
                for blocked in blocked_options:
                    seg = pick_blocked(net, origin, dest) if blocked else None
                    for seed in seeds[split]:
                        jobs.append({
                            "id": len(jobs), "split": split, "density": density, "scale": scale,
                            "rate": rate_for(density, scale, cfg), "origin": origin, "dest": dest,
                            "blocked": (seg,) if seg else (), "seed": seed, "strategies": tuple(strategies),
                            "label": f"{split} {density} {origin}>{dest} {'blocked ' + seg if seg else 'open'} seed {seed}",
                        })
    return jobs


# ----------------------------------------------------------------------------------------------- running
def make_row(job: dict, strategy: str, result) -> dict:
    m = result.metrics
    row = {"split": job["split"], "strategy": strategy, "density": job["density"], "rate": job["rate"],
           "scale": job["scale"], "origin": job["origin"], "dest": job["dest"],
           "blocked": "+".join(job["blocked"]) or "open", "seed": job["seed"],
           "switches": result.switches, "routes_sha1": result.routes_sha1}
    for col, key in METRIC_KEYS.items():
        row[col] = m.get(key)
    row["amb_finished"] = int(bool(row["amb_finished"]))
    return row


def _run_job(job: dict) -> list:
    from pegc_sumo.runner import run_scenario

    rows = []
    for strategy in job["strategies"]:
        s = Scenario(origin=job["origin"], dest=job["dest"], density="custom", rate=float(job["rate"]),
                     seed=job["seed"], blocked=tuple(job["blocked"]))
        result = run_scenario(s, strategy, label=f"b{os.getpid()}_{job['id']}_{strategy}")
        rows.append(make_row(job, strategy, result))
    return rows


def _guarded(arg):
    run_job, job = arg
    t0 = time.time()
    try:
        return "ok", job["id"], run_job(job), time.time() - t0
    except Exception:
        return "err", job["id"], traceback.format_exc(), time.time() - t0


def run_jobs(jobs: list, run_job, workers: int = 1, quiet: bool = False):
    """Run jobs (in parallel processes when workers > 1). Returns (rows sorted by job id, {job id: traceback})."""
    results, errors = {}, {}
    by_id = {j["id"]: j for j in jobs}
    start = time.time()

    def handle(res):
        kind, jid, payload, secs = res
        (results if kind == "ok" else errors)[jid] = payload
        if not quiet:
            print(f"[{len(results) + len(errors)}/{len(jobs)}] {'ok ' if kind == 'ok' else 'FAILED'} "
                  f"{by_id[jid].get('label', jid)}  ({secs:.1f} s, elapsed {time.time() - start:.0f} s)",
                  file=sys.stderr, flush=True)

    if workers <= 1:
        for job in jobs:
            handle(_guarded((run_job, job)))
    else:
        import multiprocessing as mp

        with mp.get_context("spawn").Pool(workers) as pool:
            for res in pool.imap_unordered(_guarded, [(run_job, j) for j in jobs]):
                handle(res)
    rows = [row for jid in sorted(results) for row in results[jid]]
    return rows, errors


# ----------------------------------------------------------------------------------------------- CSV
def _fmt(v) -> str:
    return "" if v is None else repr(v) if isinstance(v, float) else str(v)


def write_csv(rows: list, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow([_fmt(r.get(c)) for c in COLUMNS])


def read_csv(path: str) -> list:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            row = {}
            for col, v in rec.items():
                if col in STR_COLS:
                    row[col] = v
                elif v == "":
                    row[col] = None
                else:
                    row[col] = int(v) if col in INT_COLS else float(v)
            rows.append(row)
    return rows


# ----------------------------------------------------------------------------------------------- CLI
def _csv_list(text: str, allowed: tuple, what: str) -> tuple:
    items = tuple(x.strip() for x in text.split(",") if x.strip())
    bad = [x for x in items if x not in allowed]
    if bad or not items:
        raise SystemExit(f"unknown {what}: {bad or text!r}; allowed: {', '.join(allowed)}")
    return items


def main(argv=None, run_job=None) -> int:
    p = argparse.ArgumentParser(description="PEGC batch experiments (SUMO) -> results.csv")
    p.add_argument("--out", default=DEFAULT_RESULTS)
    p.add_argument("--calibration", default=DEFAULT_CALIBRATION, help="calibration.json made by experiments.calibrate")
    p.add_argument("--scale", type=float, default=None, help="override the calibrated traffic scale (recorded in the CSV)")
    p.add_argument("--workers", type=int, default=min(os.cpu_count() or 1, 4))
    p.add_argument("--quick", action="store_true", help="smoke grid: 1 route, no blockage, seeds 1 and 101")
    p.add_argument("--split", choices=("tune", "test", "all"), default="all")
    p.add_argument("--strategies", default=",".join(STRATEGIES))
    p.add_argument("--densities", default=",".join(DENSITIES))
    args = p.parse_args(argv)

    scale = args.scale
    if scale is None:
        if not os.path.exists(args.calibration):
            print(f"No calibration found at {args.calibration}.\nRun first:  python -m experiments.calibrate\n"
                  f"(or pass --scale X to run with an explicit traffic scale)", file=sys.stderr)
            return 2
        scale = json.load(open(args.calibration, encoding="utf-8")).get("scale")
        if scale is None:
            print("calibration.json found no traffic scale that is not oversaturated; extend the ladder or pass --scale.",
                  file=sys.stderr)
            return 2

    kw = {}
    if args.quick:
        kw.update(routes=ROUTES[:1], blocked_options=(False,), tune_seeds=(1,), test_seeds=(101,))
    jobs = build_jobs(scale, strategies=_csv_list(args.strategies, STRATEGIES, "strategy"),
                      densities=_csv_list(args.densities, DENSITIES, "density"),
                      splits=("tune", "test") if args.split == "all" else (args.split,), **kw)
    print(f"{len(jobs)} scenarios x {len(jobs[0]['strategies'])} strategies = "
          f"{len(jobs) * len(jobs[0]['strategies'])} runs, traffic scale {scale}, {args.workers} worker(s)", file=sys.stderr)
    if run_job is None:
        from pegc_sumo.sumo_io import build_static_network

        build_static_network()                 # build the SUMO network once, before any worker starts
        run_job = _run_job
    rows, errors = run_jobs(jobs, run_job, max(1, args.workers))
    if errors:
        write_csv(rows, args.out + ".partial")
        for jid, tb in sorted(errors.items()):
            print(f"\nJOB {jid} FAILED ({jobs[jid]['label']}):\n{tb}", file=sys.stderr)
        print(f"{len(errors)} job(s) failed. {args.out} was NOT written; finished rows are in {args.out}.partial",
              file=sys.stderr)
        return 1
    write_csv(rows, args.out)
    bad = sum(1 for r in rows if r["violations"])
    print(f"wrote {len(rows)} rows to {args.out}; runs with safety violations: {bad} (must be 0)")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

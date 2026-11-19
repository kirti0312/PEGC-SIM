"""Step 5, part 3: analyse results.csv (the ONLY source of numbers here).

    python -m experiments.analyze                 # tables for both seed splits, charts for the test split

Per strategy it gives mean, sample standard deviation and n, the percentage change of the mean versus Strategy A
(negative stays negative: for time, wait, queue and stops negative is better; for throughput positive is better),
and the paired difference versus A on the identical scenario and seed. Ambulance metrics use only runs where the
ambulance finished; recovery time uses only runs that recovered (the count is shown, never a zero).
A worse result is shown as worse.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys

from pegc_sumo.config import CONFIG

from .batch import ALL_STRATEGIES as STRATEGIES
from .batch import DEFAULT_RESULTS, HERE, read_csv

# column, label, which direction is better
METRICS = (("amb_time", "Ambulance travel time (s)", "lower"), ("amb_stops", "Ambulance stops", "lower"),
           ("avg_wait", "Other drivers' average wait (s)", "lower"), ("longest_wait", "Longest single wait (s)", "lower"),
           ("max_queue", "Largest queue (vehicles)", "lower"), ("throughput", "Throughput (veh/h)", "higher"),
           ("recovery_time", "Recovery time (s)", "lower"),
           ("wait_p95", "95th-percentile wait per vehicle (s)", "lower"),
           ("q60_after", "Total queue 60 s after the ambulance passed", "lower"),
           ("q120_after", "Total queue 120 s after the ambulance passed", "lower"))
AMB_ONLY = {"amb_time", "amb_stops"}
HEADLINE = ("amb_time", "avg_wait", "max_queue")
COLORS = {"A": "#7f7f7f", "B": "#1f77b4", "C": "#d95f02", "C1": "#e6ab02", "C2": "#7570b3"}
AMB_LIMIT = CONFIG.max_sim_time - CONFIG.warmup      # ambulance enters at warm-up; an unfinished run is capped here
PAIR_KEYS = ("split", "density", "origin", "dest", "blocked", "seed")


def _valid(row: dict, metric: str):
    v = row.get(metric)
    if v is None or (metric in AMB_ONLY and not row.get("amb_finished")):
        return None
    return v


def mean_std(values: list):
    n = len(values)
    if n == 0:
        return None, None
    m = sum(values) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
    return m, sd


def summarize(rows: list, level: str = "density") -> list:
    """level 'density': pool routes, blockage and seeds. level 'detail': one group per density x route x blockage."""
    keys = ("split", "density") if level == "density" else ("split", "density", "origin", "dest", "blocked")
    groups = {}
    for r in rows:
        groups.setdefault(tuple(r[k] for k in keys), []).append(r)
    out = []
    for gk in sorted(groups):
        members = groups[gk]
        base = {m["_pk"]: m for m in ({**r, "_pk": tuple(r[k] for k in PAIR_KEYS)} for r in members if r["strategy"] == "A")}
        for strat in [s for s in STRATEGIES if any(r["strategy"] == s for r in members)]:
            mine = [{**r, "_pk": tuple(r[k] for k in PAIR_KEYS)} for r in members if r["strategy"] == strat]
            rec = dict(zip(keys, gk))
            rec["strategy"] = strat
            rec["runs"] = len(mine)
            rec["violations"] = sum(r["violations"] or 0 for r in mine)
            for metric, _, _ in METRICS:
                vals = [v for v in (_valid(r, metric) for r in mine) if v is not None]
                m, sd = mean_std(vals)
                a_vals = [v for v in (_valid(r, metric) for r in base.values()) if v is not None]
                a_mean = mean_std(a_vals)[0]
                pairs = [(_valid(r, metric), _valid(base[r["_pk"]], metric)) for r in mine if r["_pk"] in base]
                diffs = [x - y for x, y in pairs if x is not None and y is not None]
                rec[f"{metric}_n"] = len(vals)
                rec[f"{metric}_mean"] = m
                rec[f"{metric}_std"] = sd
                rec[f"{metric}_pct_vs_A"] = None if (m is None or not a_mean) else (m - a_mean) / a_mean * 100.0
                rec[f"{metric}_paired_diff"] = mean_std(diffs)[0]
                rec[f"{metric}_pairs"] = len(diffs)
            # sensitivity: an ambulance that did not finish counts as the time limit instead of being dropped
            def capped(rows_):
                return [r["amb_time"] if r.get("amb_finished") and r.get("amb_time") is not None else AMB_LIMIT
                        for r in rows_]
            cap_mean = mean_std(capped(mine))[0]
            cap_a = mean_std(capped(list(base.values())))[0]
            rec["amb_finished"] = sum(1 for r in mine if r.get("amb_finished"))
            rec["amb_time_limit_mean"] = cap_mean
            rec["amb_time_limit_pct_vs_A"] = None if (cap_mean is None or not cap_a) else (cap_mean - cap_a) / cap_a * 100.0
            out.append(rec)
    return out


def grid(summary: list, split: str, cell, title: str) -> str:
    """One line per density x route x blockage cell (detail summary), one column per strategy; cell(rec) -> text."""
    recs = [r for r in summary if r["split"] == split]
    strats = [s for s in STRATEGIES if any(r["strategy"] == s for r in recs)]
    lines = [f"\n[{split} seeds] {title}", f"{'cell':<26}" + "".join(f"{s:<17}" for s in strats)]
    keys = sorted({(r["density"], r["origin"], r["dest"], r["blocked"]) for r in recs},
                  key=lambda k: (("low", "medium", "high").index(k[0]), k[1], k[3]))
    for k in keys:
        name = f"{k[0]} {k[1]}>{k[2]} {k[3]}"
        row = {r["strategy"]: r for r in recs if (r["density"], r["origin"], r["dest"], r["blocked"]) == k}
        lines.append(f"{name:<26}" + "".join(f"{(cell(row[s]) if s in row else '-'):<17}" for s in strats))
    return "\n".join(lines)


def _pct(rec, key):
    v = rec.get(key)
    return "n/a" if v is None else f"{v:+.1f}%"


def format_cell_reports(detail: list) -> str:
    out = []
    for split in sorted({r["split"] for r in detail}):
        out.append(grid(detail, split, lambda r: f"{r['amb_finished']}/{r['runs']}",
                        "ambulance FINISHED runs per cell (finished/total)"))
        out.append(grid(detail, split, lambda r: f"{r['amb_time_limit_mean']:.1f} ({_pct(r, 'amb_time_limit_pct_vs_A')})"
                        if r["amb_time_limit_mean"] is not None else "n/a",
                        f"SENSITIVITY: ambulance time, unfinished runs counted as the {AMB_LIMIT:.0f} s limit "
                        f"(mean, % vs A)"))
        out.append(grid(detail, split, lambda r: f"{r['longest_wait_mean']:.1f} ({_pct(r, 'longest_wait_pct_vs_A')})"
                        if r["longest_wait_mean"] is not None else "n/a", "longest single wait per cell (mean s, % vs A)"))
    return "\n".join(out)


def paired_vs(rows: list, strat: str, ref: str, metrics=("amb_time", "avg_wait", "longest_wait", "wait_p95", "max_queue",
                                                           "q60_after", "q120_after")) -> list:
    """Mean of (strat - ref) on the identical scenario and seed, per split and density. Positive = strat higher."""
    out = []
    for split in sorted({r["split"] for r in rows}):
        for density in ("low", "medium", "high"):
            sel = [r for r in rows if r["split"] == split and r["density"] == density]
            ref_by = {tuple(r[k] for k in PAIR_KEYS): r for r in sel if r["strategy"] == ref}
            rec = {"split": split, "density": density}
            for m in metrics:
                d = []
                for r in sel:
                    if r["strategy"] != strat:
                        continue
                    o = ref_by.get(tuple(r[k] for k in PAIR_KEYS))
                    x, y = _valid(r, m), (_valid(o, m) if o else None)
                    if x is not None and y is not None:
                        d.append(x - y)
                rec[m] = (mean_std(d)[0], len(d))
            if any(rec[m][1] for m in metrics):
                out.append(rec)
    return out


def format_paired(rows: list, strat: str, ref: str) -> str:
    recs = paired_vs(rows, strat, ref)
    if not recs:
        return ""
    ms = [m for m in recs[0] if m not in ("split", "density")]
    lines = [f"\n{strat} minus {ref}, paired on identical scenario and seed (mean difference; positive = {strat} is HIGHER = worse "
             f"for every metric here; n pairs in brackets)", f"{'':<14}" + "".join(f"{m:<18}" for m in ms)]
    for r in recs:
        lines.append(f"{r['split'] + ' ' + r['density']:<14}" + "".join(
            f"{'n/a' if r[m][0] is None else format(r[m][0], '+.2f') + ' (' + str(r[m][1]) + ')':<18}" for m in ms))
    return "\n".join(lines)


def compare_baseline(rows: list, base: list) -> tuple:
    """Re-run determinism check: for runs present in both files (same split, strategy, scenario, seed), compare every
    column the OLD file has. Returns (runs compared, identical runs, [first mismatches])."""
    key = lambda r: (r["split"], r["strategy"], r["density"], r["origin"], r["dest"], r["blocked"], r["seed"])
    new = {key(r): r for r in rows}
    cols = [c for c in base[0] if c not in ("c3_weight",)] if base else []
    n = same = 0
    bad = []
    for b in base:
        r = new.get(key(b))
        if r is None:
            continue
        n += 1
        diff = [c for c in cols if r.get(c) != b.get(c)]
        if diff:
            bad.append((key(b), diff))
        else:
            same += 1
    return n, same, bad[:5]


def write_summary(summary: list, path: str) -> None:
    if not summary:
        return
    cols = list(summary[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        for r in summary:
            w.writerow(["" if r[c] is None else (repr(r[c]) if isinstance(r[c], float) else r[c]) for c in cols])


def _cell(rec: dict, metric: str) -> str:
    m, sd, pct = rec[f"{metric}_mean"], rec[f"{metric}_std"], rec[f"{metric}_pct_vs_A"]
    if m is None:
        return "n/a"
    base = f"{m:.1f}±{sd:.1f}"
    return base if rec["strategy"] == "A" or pct is None else f"{base} ({pct:+.1f}%)"


def format_tables(summary: list) -> str:
    lines = []
    labels = {m: lab for m, lab, _ in METRICS}
    for split in sorted({r["split"] for r in summary}):
        for density in [d for d in ("low", "medium", "high") if any(r["split"] == split and r["density"] == d for r in summary)]:
            recs = [r for r in summary if r["split"] == split and r["density"] == density]
            lines.append(f"\n[{split} seeds] {density} traffic, {recs[0]['runs']} runs per strategy "
                         f"(mean±std, % change of the mean vs A)")
            head = f"{'':<4}" + "".join(f"{labels[m][:28]:<34}" for m in HEADLINE) + "recovered  violations"
            lines.append(head)
            for r in recs:
                rec_n = r["recovery_time_n"]
                lines.append(f"{r['strategy']:<4}" + "".join(f"{_cell(r, m):<34}" for m in HEADLINE)
                             + f"{rec_n}/{r['runs']:<8} {r['violations']}")
    return "\n".join(lines)


def make_charts(summary: list, split: str, outdir: str) -> list:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    recs = [r for r in summary if r["split"] == split]
    densities = [d for d in ("low", "medium", "high") if any(r["density"] == d for r in recs)]
    strategies = [s for s in STRATEGIES if any(r["strategy"] == s for r in recs)]
    paths = []
    for metric, label, better in METRICS:
        if metric in ("recovery_time", "amb_stops", "longest_wait"):
            continue
        fig, ax = plt.subplots(figsize=(7.5, 4.2))
        width = 0.8 / max(1, len(strategies))
        for i, s in enumerate(strategies):
            means, sds = [], []
            for d in densities:
                r = next((x for x in recs if x["density"] == d and x["strategy"] == s), None)
                means.append(r[f"{metric}_mean"] or 0.0 if r else 0.0)
                sds.append(r[f"{metric}_std"] or 0.0 if r else 0.0)
            ax.bar([x + i * width for x in range(len(densities))], means, width, yerr=sds, capsize=2, label=s,
                   color=COLORS.get(s))
        ax.set_xticks([x + width * (len(strategies) - 1) / 2 for x in range(len(densities))], densities)
        ax.set_ylabel(label)
        ax.set_title(f"SUMO results ({split} seeds, {better} is better); bars = mean, whiskers = std", fontsize=9)
        ax.legend(title="Strategy", fontsize=8)
        fig.tight_layout()
        path = os.path.join(outdir, f"chart_{metric}.png")
        fig.savefig(path, dpi=130)
        plt.close(fig)
        paths.append(path)
    return paths


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Analyse results.csv")
    p.add_argument("--csv", default=DEFAULT_RESULTS)
    p.add_argument("--outdir", default=HERE)
    p.add_argument("--split", default="test", help="seed split used for the charts")
    p.add_argument("--no-charts", action="store_true")
    p.add_argument("--baseline", default=None, help="frozen results.csv: check that shared runs are unchanged")
    args = p.parse_args(argv)
    if not os.path.exists(args.csv):
        print(f"{args.csv} not found; run python -m experiments.batch first", file=sys.stderr)
        return 2
    rows = read_csv(args.csv)
    print(f"SUMO results from {args.csv}: {len(rows)} runs. (Not browser mini-simulator results.)")
    by_density, detail = summarize(rows, "density"), summarize(rows, "detail")
    write_summary(by_density, os.path.join(args.outdir, "summary_by_density.csv"))
    write_summary(detail, os.path.join(args.outdir, "summary_detail.csv"))
    print(format_tables(by_density))
    print(format_cell_reports(detail))
    for strat, ref in (("C3", "C"), ("C", "A")):
        if any(r["strategy"] == strat for r in rows):
            print(format_paired(rows, strat, ref))
    if args.baseline:
        n, same, bad = compare_baseline(rows, read_csv(args.baseline))
        print(f"\nBaseline check vs {args.baseline}: {same}/{n} shared runs identical in every old column"
              + ("" if n == same else f"; first differences: {bad}"))
    bad = sum(r["violations"] or 0 for r in rows)
    print(f"\nTotal safety violations over all runs: {bad} (must be 0)")
    if not args.no_charts:
        try:
            charts = make_charts(by_density, args.split, args.outdir)
            print("charts:", ", ".join(os.path.basename(c) for c in charts))
        except ImportError:
            print("matplotlib is not installed: charts skipped (pip install matplotlib)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

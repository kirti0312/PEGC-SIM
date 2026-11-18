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

from .batch import DEFAULT_RESULTS, HERE, STRATEGIES, read_csv

# column, label, which direction is better
METRICS = (("amb_time", "Ambulance travel time (s)", "lower"), ("amb_stops", "Ambulance stops", "lower"),
           ("avg_wait", "Other drivers' average wait (s)", "lower"), ("longest_wait", "Longest single wait (s)", "lower"),
           ("max_queue", "Largest queue (vehicles)", "lower"), ("throughput", "Throughput (veh/h)", "higher"),
           ("recovery_time", "Recovery time (s)", "lower"))
AMB_ONLY = {"amb_time", "amb_stops"}
HEADLINE = ("amb_time", "avg_wait", "max_queue")
COLORS = {"A": "#7f7f7f", "B": "#1f77b4", "C": "#d95f02", "C1": "#e6ab02", "C2": "#7570b3"}
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
            out.append(rec)
    return out


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

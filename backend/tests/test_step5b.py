"""Step 5b tests (pure Python, no SUMO): C unchanged, C3 differs only via its parameter, new metrics on synthetic data,
reporting additions, tuning rule, frozen baseline."""
import hashlib
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from experiments import analyze, batch, tune_c3
from pegc_sumo.config import CONFIG
from pegc_sumo.metrics import MetricsRecorder, percentile
from pegc_sumo.network import build_network
from pegc_sumo.strategies import ApproachData, evaluate, make_strategy

from .test_step4 import make_ctx, red_for_ambulance, u

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")


# ----------------------------------------------------------------------------- C unchanged, C3 only via its parameter
def decide_with(cfg, c3, longest=40.0, cars=2):
    ctx, bank, tr, log, data = make_ctx()
    ctx.cfg = cfg
    data.approach["H"] = [ApproachData(cars, 0.0, [(10.0, 0.0, longest)])]
    sig = red_for_ambulance(bank)
    req, passed, eta = evaluate(ctx, sig, u(d_stop=150.0, n=3), 0, True, c3)
    info = [e for e in log if len(e) == 4][0][3]
    return req, passed, eta, sig.c_mode, info


def test_c_ignores_the_c3_parameter():
    base = decide_with(CONFIG, False)
    for w in (0.0, 5.0, 100.0):
        assert decide_with(replace(CONFIG, c3_wait_weight=w), False) == base


def test_c3_default_equals_c_exactly():
    assert CONFIG.c3_wait_weight == CONFIG.c_wait_weight
    assert decide_with(CONFIG, True) == decide_with(CONFIG, False)


def test_c3_differs_only_through_the_weight_and_only_in_cost():
    c = decide_with(CONFIG, False)
    c3 = decide_with(replace(CONFIG, c3_wait_weight=8.0), True)
    assert c[3] == "active" and c3[3] == "declined"                  # long cross wait now outweighs the benefit
    extra = (8.0 - CONFIG.c_wait_weight) * 40.0
    assert c3[4]["cost"] == pytest.approx(c[4]["cost"] + extra)
    for k in ("eta", "n", "nUsed", "lead", "delaySaved", "benefit", "crossCars", "tStartIn"):
        assert c3[4][k] == c[4][k], k
    # with no cross wait at all the weight cannot matter
    assert decide_with(replace(CONFIG, c3_wait_weight=8.0), True, longest=0.0) == decide_with(CONFIG, False, longest=0.0)


def test_make_strategy_c3_is_a_full_c_with_the_flag():
    s = make_strategy("C3")
    assert (s.id, s.queue_aware, s.recovery, s.c3) == ("C3", True, True, True)
    assert make_strategy("C").c3 is False
    with pytest.raises(ValueError):
        make_strategy("C4")


# ----------------------------------------------------------------------------- new metrics on synthetic data
def test_percentile_nearest_rank():
    v = [float(i) for i in range(1, 101)]
    assert percentile(v, 95.0) == 95.0 and percentile(v, 100.0) == 100.0 and percentile(v, 1.0) == 1.0
    assert percentile([3.0, 9.0], 95.0) == 9.0 and percentile([], 95.0) is None


def test_recorder_wait_p95_counts_finished_and_live_vehicles():
    rec = MetricsRecorder(build_network(), CONFIG)
    rec.done_waits = [float(i) for i in range(1, 20)]      # 19 finished vehicles with waits 1..19 s
    rec.live = {"live1"}
    rec.wait = {"live1": 50.0}
    assert rec.metrics()["waitP95"] == 19.0                # 20 vehicles: rank ceil(0.95*20)=19 is the 19th smallest
    rec.wait["live1"] = 0.0
    rec.done_waits = [0.0] * 18 + [30.0, 80.0]             # 19th smallest of 21 vehicles incl. the live one
    assert rec.metrics()["waitP95"] == 30.0


def fake_result(log, series):
    return SimpleNamespace(log=log, series=series, metrics={}, switches=0, routes_sha1="x")


def test_queue_after_pass_reads_log_and_series():
    series = [[float(t), t] for t in range(0, 421)]                 # queue == time, 1 s samples
    log = [{"kind": "pass", "data": {"at": 99.0}},                  # per-junction pass of C: ignored
           {"kind": "pass", "data": {"time": 150.0}}]
    r = fake_result(log, series)
    assert batch.queue_after_pass(r, 60.0) == 210 and batch.queue_after_pass(r, 120.0) == 270
    assert batch.queue_after_pass(fake_result(log, series[:200]), 60.0) is None      # run ended too early
    assert batch.queue_after_pass(fake_result([], series), 60.0) is None             # never cleared


def test_split_entry_and_columns():
    assert batch.split_entry("A") == ("A", None)
    assert batch.split_entry("C3@2.0") == ("C3", 2.0)
    assert batch.split_entry("C3", 4.0) == ("C3", 4.0) and batch.split_entry("C3") == ("C3", CONFIG.c3_wait_weight)
    with pytest.raises(ValueError):
        batch.split_entry("C@2")
    assert batch.COLUMNS[:23] == ["split", "strategy", "density", "rate", "scale", "origin", "dest", "blocked", "seed",
        "amb_finished", "amb_time", "amb_stops", "avg_wait", "longest_wait", "max_queue", "throughput", "completed",
        "entered", "recovery_time", "recovery_baseline", "violations", "switches", "routes_sha1"]
    assert batch.COLUMNS[23:] == ["wait_p95", "q60_after", "q120_after", "c3_weight"]


def test_make_row_has_new_columns():
    r = fake_result([{"kind": "pass", "data": {"time": 100.0}}], [[160.0, 7], [220.0, 5]])
    r.metrics = {"waitP95": 12.5, "ambFinished": True}
    job = {"split": "test", "density": "low", "rate": 126, "scale": 0.7, "origin": "W0", "dest": "E1", "blocked": (), "seed": 1}
    row = batch.make_row(job, "C3", r, c3_weight=2.0)
    assert (row["wait_p95"], row["q60_after"], row["q120_after"], row["c3_weight"]) == (12.5, 7, 5, 2.0)


# ----------------------------------------------------------------------------- reporting
def row(strategy, seed, finished, amb_time, **kw):
    base = dict(split="test", strategy=strategy, density="high", origin="W0", dest="E1", blocked="open", seed=seed,
                amb_finished=int(finished), amb_time=amb_time, amb_stops=1, avg_wait=10.0, longest_wait=20.0,
                max_queue=5, throughput=1000.0, recovery_time=None, violations=0, wait_p95=30.0, q60_after=4, q120_after=3)
    base.update(kw)
    return base


def test_finished_counts_and_time_limit_sensitivity():
    rows = [row("A", 1, True, 100.0), row("A", 2, False, None), row("C", 1, True, 90.0), row("C", 2, True, 150.0)]
    s = {r["strategy"]: r for r in analyze.summarize(rows, "detail")}
    assert (s["A"]["amb_finished"], s["C"]["amb_finished"]) == (1, 2)
    assert s["A"]["amb_time_mean"] == 100.0                                   # unfinished dropped
    assert s["A"]["amb_time_limit_mean"] == (100.0 + analyze.AMB_LIMIT) / 2   # unfinished counted as the limit
    assert analyze.AMB_LIMIT == 360.0
    assert s["C"]["amb_time_limit_mean"] == 120.0
    assert s["C"]["amb_time_limit_pct_vs_A"] == pytest.approx((120.0 - 230.0) / 230.0 * 100)     # negative stays negative
    text = analyze.format_cell_reports(analyze.summarize(rows, "detail"))
    assert "1/2" in text and "2/2" in text and "SENSITIVITY" in text


def test_new_metrics_summarised_and_paired_c3_vs_c():
    rows = [row(s, sd, True, 100.0, wait_p95=p, q120_after=q, longest_wait=lw)
            for s, p, q, lw in (("A", 30.0, 3, 20.0), ("C", 40.0, 6, 30.0), ("C3", 35.0, 4, 25.0)) for sd in (1, 2)]
    s = {r["strategy"]: r for r in analyze.summarize(rows)}
    assert s["C"]["wait_p95_mean"] == 40.0 and s["C"]["wait_p95_pct_vs_A"] == pytest.approx(33.333333, rel=1e-5)
    assert s["C3"]["q120_after_pct_vs_A"] == pytest.approx(33.333333, rel=1e-5)
    d = analyze.paired_vs(rows, "C3", "C")[0]
    assert d["wait_p95"] == (-5.0, 2) and d["q120_after"] == (-2.0, 2) and d["longest_wait"] == (-5.0, 2)
    assert "C3 minus C" in analyze.format_paired(rows, "C3", "C")


def test_old_csv_without_new_columns_still_analyses():
    rows = [{k: v for k, v in row("A", 1, True, 100.0).items() if k not in ("wait_p95", "q60_after", "q120_after")}]
    rec = analyze.summarize(rows)[0]
    assert rec["wait_p95_mean"] is None and rec["amb_finished"] == 1


def test_compare_baseline_detects_a_change():
    base = [row("A", 1, True, 100.0), row("C", 1, True, 90.0)]
    same = [dict(r) for r in base]
    assert analyze.compare_baseline(same, base) == (2, 2, [])
    changed = [dict(r) for r in base]
    changed[1]["amb_time"] = 91.0
    n, ok, bad = analyze.compare_baseline(changed, base)
    assert (n, ok) == (2, 1) and bad[0][1] == ["amb_time"]


# ----------------------------------------------------------------------------- tuning rule
def test_tuning_choice_rule():
    def r(strategy, w, amb, lw):
        return row(strategy, 1, True, amb, longest_wait=lw, c3_weight=w)
    rows = [r("A", None, 150.0, 40.0), r("C", None, 100.0, 50.0), r("C3", 1.0, 105.0, 45.0), r("C3", 2.0, 110.0, 40.0),
            r("C3", 4.0, 120.0, 38.0), r("C3", 8.0, 140.0, 30.0)]
    tab = tune_c3.table(rows, (1.0, 2.0, 4.0, 8.0))
    assert tune_c3.choose(tab) == {"weight": 2.0, "status": "ok"}              # feasible: 2, 4, 8; fastest ambulance: 2
    rows2 = [r("A", None, 150.0, 20.0)] + [x for x in rows[1:]]
    assert tune_c3.choose(tune_c3.table(rows2, (1.0, 2.0, 4.0, 8.0))) == {"weight": 8.0, "status": "constraint_not_met"}


def test_tune_main_with_fake_jobs(tmp_path):
    cal = tmp_path / "cal.json"
    cal.write_text('{"scale": 0.7}')

    def fake(job):
        rows = []
        for e in job["strategies"]:
            st, w = batch.split_entry(e)
            rows.append(row(st, job["seed"], True, 100.0, density=job["density"], origin=job["origin"], dest=job["dest"],
                            blocked="+".join(job["blocked"]) or "open", c3_weight=w, split="tune",
                            longest_wait=20.0 + (w or 0)))
        return rows
    assert tune_c3.main(["--calibration", str(cal), "--outdir", str(tmp_path), "--workers", "1"], run_job=fake) == 0
    assert (tmp_path / "c3_tuning.json").exists() and len(open(tmp_path / "c3_tuning_runs.csv").readlines()) == 60 * 6 + 1


# ----------------------------------------------------------------------------- frozen Step 5 baseline
def test_frozen_baseline_matches_checksums_and_is_readable():
    d = os.path.join(ROOT, "baselines", "step5")
    if not os.path.exists(os.path.join(d, "SHA256SUMS")):
        pytest.skip("baselines/step5 not present")
    for line in open(os.path.join(d, "SHA256SUMS")):
        digest, name = line.split()
        assert hashlib.sha256(open(os.path.join(d, name), "rb").read()).hexdigest() == digest, name
    rows = batch.read_csv(os.path.join(d, "results.csv"))
    assert len(rows) == 900
    s = analyze.summarize([r for r in rows if r["split"] == "test"], "detail")
    a = [r for r in s if r["strategy"] == "A" and r["density"] == "high" and r["blocked"] != "open" and r["origin"] == "W0"][0]
    assert a["amb_finished"] == 6 and a["runs"] == 10            # the user's finding: 4/10 unfinished

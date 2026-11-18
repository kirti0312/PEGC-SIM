"""Step 5 tests: calibration, batch experiments, CSV, analysis.

Everything except the SUMO runs themselves is tested here with injected fake runners (pure Python). The SUMO tests at
the bottom skip themselves when SUMO is not installed and run in Codespaces.
"""
import json
import math
import os
from types import SimpleNamespace

import pytest

from experiments import analyze, batch, calibrate
from pegc_sumo.config import CONFIG
from pegc_sumo.network import build_network, find_route


# --------------------------------------------------------------------------------------- helpers
def fake_result(job, strategy, amb_time=100.0, **over):
    m = {"ambFinished": True, "ambTravelTime": amb_time, "ambStops": 1, "avgWait": 20.0, "longestWait": 60.0,
         "maxQueue": 10, "throughput": 2000.0, "completed": 300, "entered": 350, "recoveryTime": None,
         "recoveryBaseline": 12.0, "violations": 0}
    m.update(over)
    return SimpleNamespace(metrics=m, switches=40, routes_sha1="sha_" + str(job["id"]))


def fake_job(job):
    return [batch.make_row(job, s, fake_result(job, s, amb_time=100.0 + len(s))) for s in job["strategies"]]


def row(strategy, seed, amb_time=100.0, split="test", density="medium", origin="W0", dest="E1", blocked="open", **kw):
    r = {"split": split, "strategy": strategy, "density": density, "rate": 231, "scale": 0.7, "origin": origin,
         "dest": dest, "blocked": blocked, "seed": seed, "amb_finished": 1, "amb_time": amb_time, "amb_stops": 1,
         "avg_wait": 20.0, "longest_wait": 60.0, "max_queue": 10, "throughput": 2000.0, "completed": 300,
         "entered": 350, "recovery_time": None, "recovery_baseline": 12.0, "violations": 0, "switches": 40,
         "routes_sha1": "x"}
    r.update(kw)
    return r


# --------------------------------------------------------------------------------------- grid
def test_rate_for_scales_browser_presets_and_clamps():
    assert [batch.rate_for(d, 1.0) for d in batch.DENSITIES] == [180, 330, 520]
    assert [batch.rate_for(d, 0.7) for d in batch.DENSITIES] == [126, 231, 364]
    assert batch.rate_for("low", 0.4) == CONFIG.rate_min          # 72 would be below the runner's minimum
    assert batch.rate_for("high", 2.0) == CONFIG.rate_max


def test_pick_blocked_leaves_a_different_valid_route():
    net = build_network()
    for origin, dest in batch.ROUTES:
        seg = batch.pick_blocked(net, origin, dest)
        normal = find_route(net, origin, dest)
        alt = find_route(net, origin, dest, frozenset({seg}))
        assert seg in [e.seg_id for e in normal]
        assert alt is not None and [e.id for e in alt] != [e.id for e in normal]
    assert len(batch.ROUTES) >= 2 and len(set(batch.ROUTES)) == len(batch.ROUTES)


def test_full_grid_is_complete_and_seeds_are_split():
    jobs = batch.build_jobs(0.7)
    assert len(jobs) == 180 and sum(len(j["strategies"]) for j in jobs) == 900
    assert all(j["strategies"] == ("A", "B", "C", "C1", "C2") for j in jobs)
    assert [j["id"] for j in jobs] == list(range(180))
    tune = {j["seed"] for j in jobs if j["split"] == "tune"}
    test = {j["seed"] for j in jobs if j["split"] == "test"}
    assert tune == {1, 2, 3, 4, 5} and test == set(range(101, 111)) and not tune & test
    assert {j["density"] for j in jobs} == {"low", "medium", "high"}
    assert {(j["origin"], j["dest"]) for j in jobs} == set(batch.ROUTES)
    assert sum(1 for j in jobs if j["blocked"]) == 90
    assert all(j["scale"] == 0.7 and j["rate"] == batch.rate_for(j["density"], 0.7) for j in jobs)
    # two workers must never share a scenario (they would share one routes file)
    keys = {(j["rate"], j["seed"], j["origin"], j["dest"], j["blocked"]) for j in jobs}
    assert len(keys) == len(jobs)
    assert batch.build_jobs(0.7) == jobs                              # deterministic order


def test_quick_grid_is_small():
    jobs = batch.build_jobs(0.7, routes=batch.ROUTES[:1], blocked_options=(False,), tune_seeds=(1,), test_seeds=(101,))
    assert len(jobs) == 6 and {j["seed"] for j in jobs} == {1, 101}


# --------------------------------------------------------------------------------------- running and CSV
def test_make_row_maps_metrics_and_blank_recovery():
    job = batch.build_jobs(0.7, routes=batch.ROUTES[:1], blocked_options=(True,), tune_seeds=(1,), test_seeds=(),
                           splits=("tune",), densities=("low",))[0]
    r = batch.make_row(job, "C1", fake_result(job, "C1", ambFinished=False, ambTravelTime=None))
    assert r["strategy"] == "C1" and r["blocked"] == "J01|J02" and r["amb_finished"] == 0 and r["amb_time"] is None
    assert r["recovery_time"] is None and r["switches"] == 40 and r["scale"] == 0.7 and set(r) == set(batch.COLUMNS)


def test_run_jobs_sorts_rows_and_reports_failures_without_losing_the_rest():
    jobs = batch.build_jobs(0.7, routes=batch.ROUTES[:1], blocked_options=(False,), tune_seeds=(1, 2), test_seeds=(),
                            splits=("tune",), densities=("low", "medium"))

    def flaky(job):
        if job["id"] == 2:
            raise RuntimeError("boom")
        return fake_job(job)

    rows, errors = batch.run_jobs(jobs, flaky, workers=1, quiet=True)
    assert list(errors) == [2] and "boom" in errors[2]
    assert len(rows) == 3 * 5
    ids = [(r["density"], r["seed"]) for r in rows[::5]]
    assert ids == [("low", 1), ("low", 2), ("medium", 2)]
    assert [r["strategy"] for r in rows[:5]] == ["A", "B", "C", "C1", "C2"]


def test_csv_round_trip_and_byte_identical_rewrites(tmp_path):
    rows = [row("A", 1, amb_time=143.75, recovery_time=33.25), row("C", 1, amb_time=None, amb_finished=0)]
    p1, p2 = tmp_path / "a.csv", tmp_path / "b.csv"
    batch.write_csv(rows, str(p1))
    back = batch.read_csv(str(p1))
    assert back[0]["amb_time"] == 143.75 and back[0]["recovery_time"] == 33.25 and back[0]["seed"] == 1
    assert back[1]["amb_time"] is None and back[1]["recovery_time"] is None and back[1]["amb_finished"] == 0
    assert isinstance(back[0]["rate"], int) and isinstance(back[0]["scale"], float) and back[0]["blocked"] == "open"
    batch.write_csv(rows, str(p2))                                  # same rows, written again: identical bytes
    assert p1.read_bytes() == p2.read_bytes()
    batch.write_csv(back, str(tmp_path / "c.csv"))                  # values survive a read-write-read round trip
    assert batch.read_csv(str(tmp_path / "c.csv")) == back
    assert p1.read_text().splitlines()[0] == ",".join(batch.COLUMNS)


def test_batch_main_needs_a_calibration(tmp_path, capsys):
    out = tmp_path / "r.csv"
    assert batch.main(["--out", str(out), "--calibration", str(tmp_path / "none.json")], run_job=fake_job) == 2
    assert "calibrate" in capsys.readouterr().err and not out.exists()
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps({"scale": None, "status": "no_scale_passed"}))
    assert batch.main(["--out", str(out), "--calibration", str(cal)], run_job=fake_job) == 2
    assert not out.exists()


def test_batch_main_writes_the_grid_with_the_calibrated_scale(tmp_path, capsys):
    cal, out = tmp_path / "cal.json", tmp_path / "r.csv"
    cal.write_text(json.dumps({"scale": 0.65}))
    assert batch.main(["--quick", "--workers", "1", "--out", str(out), "--calibration", str(cal)], run_job=fake_job) == 0
    rows = batch.read_csv(str(out))
    assert len(rows) == 6 * 5 and {r["scale"] for r in rows} == {0.65}
    assert {r["rate"] for r in rows if r["density"] == "medium"} == {batch.rate_for("medium", 0.65)}
    assert "violations: 0" in capsys.readouterr().out
    by_scenario = {}
    for r in rows:
        by_scenario.setdefault((r["split"], r["density"], r["seed"]), []).append(r)
    assert all(len({x["routes_sha1"] for x in v}) == 1 and len(v) == 5 for v in by_scenario.values())   # same traffic for all
    # --scale overrides, filters work
    out2 = tmp_path / "r2.csv"
    assert batch.main(["--quick", "--scale", "0.5", "--workers", "1", "--split", "test", "--strategies", "A,C",
                       "--densities", "high", "--out", str(out2)], run_job=fake_job) == 0
    rows2 = batch.read_csv(str(out2))
    assert [(r["split"], r["strategy"], r["density"], r["scale"]) for r in rows2] == [("test", "A", "high", 0.5), ("test", "C", "high", 0.5)]


def test_batch_main_does_not_write_a_partial_csv_as_final(tmp_path, capsys):
    out = tmp_path / "r.csv"

    def flaky(job):
        if job["id"] == 1:
            raise RuntimeError("sumo crashed")
        return fake_job(job)

    assert batch.main(["--quick", "--scale", "0.7", "--workers", "1", "--out", str(out)], run_job=flaky) == 1
    assert not out.exists() and os.path.exists(str(out) + ".partial")
    assert "sumo crashed" in capsys.readouterr().err


def test_batch_main_fails_when_a_run_had_safety_violations(tmp_path, capsys):
    def bad(job):
        return [batch.make_row(job, s, fake_result(job, s, violations=2)) for s in job["strategies"]]

    assert batch.main(["--quick", "--scale", "0.7", "--workers", "1", "--out", str(tmp_path / "r.csv")], run_job=bad) == 1
    assert "must be 0" in capsys.readouterr().out


def test_batch_rejects_unknown_strategy_or_density():
    with pytest.raises(SystemExit):
        batch.main(["--strategies", "A,Z", "--scale", "0.7"], run_job=fake_job)
    with pytest.raises(SystemExit):
        batch.main(["--densities", "huge", "--scale", "0.7"], run_job=fake_job)


# --------------------------------------------------------------------------------------- calibration
def test_queue_growth_compares_late_with_middle_of_the_run():
    flat = [[t, 10] for t in range(0, 421, 4)]
    assert calibrate.queue_growth(flat) == 0
    rising = [[float(t), 10.0 if t <= 240 else 20.0] for t in range(0, 421, 4)]
    assert calibrate.queue_growth(rising) == 10.0
    with pytest.raises(ValueError):
        calibrate.queue_growth([[0.0, 1], [60.0, 2]])


def test_choose_scale_takes_the_largest_contiguous_passing_scale():
    assert calibrate.choose_scale([(0.4, 1), (0.5, 2), (0.6, 5), (0.7, 9), (0.8, 3)], 6.0) == 0.6   # 0.8 passes but 0.7 fails
    assert calibrate.choose_scale([(0.4, 7), (0.5, 8)], 6.0) is None
    assert calibrate.choose_scale([(0.6, 1), (0.4, 1), (0.5, 6.0)], 6.0) == 0.6                 # unsorted input, boundary passes
    assert calibrate.LADDER[0] == 0.4 and calibrate.LADDER[-1] == 1.0 and len(calibrate.LADDER) == 13
    assert not set(calibrate.CAL_SEEDS) & (set(batch.TUNE_SEEDS) | set(batch.TEST_SEEDS))


def _cal_row(job, growth):
    return [{"scale": job["scale"], "density": job["density"], "rate": job["rate"], "seed": job["seed"],
             "growth": growth, "early_queue": 10.0, "late_queue": 10.0 + growth, "max_queue": 14, "avg_wait": 12.0,
             "throughput": 2100.0, "entered": 350, "completed": 330, "violations": 0}]


def test_calibrate_main_records_how_the_scale_was_chosen(tmp_path, capsys):
    def fake(job):   # medium grows with the scale; low is calm; high is oversaturated
        g = {"medium": 40 * (job["scale"] - 0.5) - 1, "low": 0.5, "high": 50.0}[job["density"]]
        return _cal_row(job, g)

    out = tmp_path / "cal.json"
    rc = calibrate.main(["--out", str(out), "--ladder", "0.5,0.6,0.7,0.8", "--seeds", "201,202", "--workers", "1"], run_job=fake)
    cal = json.loads(out.read_text())
    assert rc == 0 and cal["scale"] == 0.6 and cal["status"] == "ok" and cal["growth_max"] == 6.0
    assert [t["scale"] for t in cal["ladder"]] == [0.5, 0.6, 0.7, 0.8] and [t["passes"] for t in cal["ladder"]] == [True, True, False, False]
    assert cal["ladder"][1]["mean_growth"] == pytest.approx(3.0) and cal["ladder"][1]["seeds"] == [201, 202]
    assert {d: p["rate"] for d, p in cal["presets"].items()} == {"low": 108, "medium": 198, "high": 312}
    assert cal["presets"]["low"]["browser_rate"] == 180 and not cal["presets"]["low"]["clamped"]
    assert cal["preset_checks"]["high"]["saturated"] is True and cal["preset_checks"]["medium"]["saturated"] is False
    text = capsys.readouterr().out
    assert "Chosen scale: 0.6" in text and "STILL SATURATED" in text
    # the batch accepts this file as is
    res = tmp_path / "r.csv"
    assert batch.main(["--quick", "--workers", "1", "--out", str(res), "--calibration", str(out)], run_job=fake_job) == 0
    assert {r["scale"] for r in batch.read_csv(str(res))} == {0.6}


def test_calibrate_main_reports_when_nothing_passes_or_everything_passes(tmp_path, capsys):
    out = tmp_path / "cal.json"
    assert calibrate.main(["--out", str(out), "--ladder", "0.5,0.6", "--seeds", "201", "--workers", "1"],
                          run_job=lambda j: _cal_row(j, 100.0)) == 1
    cal = json.loads(out.read_text())
    assert cal["scale"] is None and cal["status"] == "no_scale_passed" and cal["presets"] == {}
    assert "NO scale passed" in capsys.readouterr().out
    assert batch.main(["--quick", "--out", str(tmp_path / "r.csv"), "--calibration", str(out)], run_job=fake_job) == 2
    assert calibrate.main(["--out", str(out), "--ladder", "0.5,1.0", "--seeds", "201", "--workers", "1"],
                          run_job=lambda j: _cal_row(j, -5.0)) == 0
    cal = json.loads(out.read_text())
    assert cal["scale"] == 1.0 and cal["status"] == "all_passed"
    assert {d: p["rate"] for d, p in cal["presets"].items()} == {"low": 180, "medium": 330, "high": 520}


# --------------------------------------------------------------------------------------- analysis
def test_analyze_statistics_percent_change_and_pairing():
    rows = [row("A", 101, 100.0), row("A", 102, 110.0), row("C", 101, 80.0), row("C", 102, 90.0),
            row("B", 101, 120.0, amb_finished=0), row("B", 102, 130.0)]
    rec = {r["strategy"]: r for r in analyze.summarize(rows, "density")}
    assert rec["A"]["amb_time_mean"] == 105.0 and rec["A"]["amb_time_std"] == pytest.approx(math.sqrt(50.0))
    assert rec["C"]["amb_time_mean"] == 85.0 and rec["C"]["amb_time_pct_vs_A"] == pytest.approx(-19.0476, abs=1e-3)
    assert rec["C"]["amb_time_paired_diff"] == -20.0 and rec["C"]["amb_time_pairs"] == 2
    assert rec["A"]["amb_time_pct_vs_A"] == 0.0
    assert rec["B"]["amb_time_n"] == 1 and rec["B"]["amb_time_mean"] == 130.0       # unfinished ambulance run is excluded
    assert rec["B"]["amb_time_pct_vs_A"] > 0                                         # worse stays positive (worse)
    assert rec["C"]["recovery_time_n"] == 0 and rec["C"]["recovery_time_mean"] is None   # never shown as zero
    assert "-19.0%" in analyze._cell(rec["C"], "amb_time") and "±" in analyze._cell(rec["A"], "amb_time")


def test_analyze_throughput_recovery_groups_and_splits():
    rows = [row("A", 1, split="tune", throughput=2000.0, recovery_time=30.0), row("C", 1, split="tune", throughput=2200.0, recovery_time=20.0),
            row("A", 101, throughput=1000.0), row("C", 101, throughput=900.0),
            row("A", 101, origin="W1", dest="E0", blocked="J00|J01", amb_time=50.0),
            row("C", 101, origin="W1", dest="E0", blocked="J00|J01", amb_time=40.0, recovery_time=15.0)]
    dens = analyze.summarize(rows, "density")
    assert {(r["split"], r["strategy"]) for r in dens} == {("tune", "A"), ("tune", "C"), ("test", "A"), ("test", "C")}
    tune_c = next(r for r in dens if r["split"] == "tune" and r["strategy"] == "C")
    assert tune_c["throughput_pct_vs_A"] == pytest.approx(10.0) and tune_c["recovery_time_pct_vs_A"] == pytest.approx(-33.3333, abs=1e-3)
    test_c = next(r for r in dens if r["split"] == "test" and r["strategy"] == "C")
    assert test_c["throughput_n"] == 2 and test_c["recovery_time_n"] == 1
    detail = analyze.summarize(rows, "detail")
    assert len([r for r in detail if r["split"] == "test" and r["strategy"] == "C"]) == 2    # open W0>E1, blocked W1>E0
    assert all(r["violations"] == 0 for r in dens)


def test_analyze_main_reads_only_the_csv_and_writes_summaries_and_charts(tmp_path, capsys):
    pytest.importorskip("matplotlib")
    rows = [row(s, seed, amb_time=100.0 + 10 * i + seed % 7, density=d, split=sp)
            for sp, seeds in (("tune", (1, 2)), ("test", (101, 102))) for d in ("low", "medium", "high")
            for seed in seeds for i, s in enumerate(batch.STRATEGIES)]
    src = tmp_path / "results.csv"
    batch.write_csv(rows, str(src))
    assert analyze.main(["--csv", str(src), "--outdir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "Not browser mini-simulator results" in text and "[test seeds] high traffic" in text and "violations over all runs: 0" in text
    for name in ("summary_by_density.csv", "summary_detail.csv", "chart_amb_time.png", "chart_avg_wait.png",
                 "chart_max_queue.png", "chart_throughput.png"):
        assert (tmp_path / name).stat().st_size > 0
    again = tmp_path / "again"
    again.mkdir()
    assert analyze.main(["--csv", str(src), "--outdir", str(again), "--no-charts"]) == 0
    assert (tmp_path / "summary_by_density.csv").read_bytes() == (again / "summary_by_density.csv").read_bytes()
    assert analyze.main(["--csv", str(tmp_path / "missing.csv")]) == 2


# --------------------------------------------------------------------------------------- glue to the runner (stubbed)
def test_run_job_builds_identical_scenarios_for_every_strategy(monkeypatch):
    import pegc_sumo.runner as runner

    calls = []

    def stub(s, strategy="A", label=None, cfg=CONFIG):
        calls.append((s, strategy, label))
        return fake_result({"id": 7}, strategy)

    monkeypatch.setattr(runner, "run_scenario", stub)
    job = batch.build_jobs(0.7, routes=batch.ROUTES[1:], blocked_options=(True,), tune_seeds=(), test_seeds=(103,),
                           splits=("test",), densities=("high",))[0]
    rows = batch._run_job(job)
    assert [c[1] for c in calls] == ["A", "B", "C", "C1", "C2"] and [r["strategy"] for r in rows] == ["A", "B", "C", "C1", "C2"]
    scenarios = {(s.origin, s.dest, s.density, s.rate, s.seed, s.blocked) for s, _, _ in calls}
    assert scenarios == {("W1", "E0", "custom", 364.0, 103, ("J00|J01",))}        # one scenario, shared by all strategies
    assert len({c[2] for c in calls}) == 5                                         # unique TraCI labels
    assert all(r["rate"] == 364 and r["blocked"] == "J00|J01" for r in rows)


def test_calibration_job_measures_growth_from_the_queue_series(monkeypatch):
    import pegc_sumo.runner as runner

    seen = []

    def stub(s, strategy="A", label=None, cfg=CONFIG):
        seen.append((s, strategy))
        res = fake_result({"id": 1}, strategy)
        res.series = [[float(t), 10.0 if t <= 240 else 25.0] for t in range(0, 421, 4)]
        return res

    monkeypatch.setattr(runner, "run_scenario", stub)
    out = calibrate._calib_job({"id": 3, "scale": 0.6, "density": "medium", "rate": 198, "seed": 201})
    assert out[0]["growth"] == 15.0 and out[0]["scale"] == 0.6 and out[0]["seed"] == 201 and out[0]["violations"] == 0
    s, strategy = seen[0]
    assert strategy == "A" and (s.origin, s.dest, s.density, s.rate, s.seed) == ("W0", "E1", "custom", 198.0, 201)


def test_run_jobs_parallel_pool_plumbing_with_picklable_functions():
    jobs = [{"id": i, "label": f"job {i}"} for i in range(6)]
    rows, errors = batch.run_jobs(jobs, list, workers=2, quiet=True)               # list(job) -> its keys
    assert not errors and rows == [k for j in jobs for k in j]                     # collected in job order
    rows, errors = batch.run_jobs(jobs, int, workers=2, quiet=True)                # int(dict) raises inside the worker
    assert rows == [] and sorted(errors) == list(range(6)) and "TypeError" in errors[0]


# --------------------------------------------------------------------------------------- SUMO (skipped without SUMO)
def _sumo_available():
    try:
        import sumolib  # noqa: F401
        import traci  # noqa: F401

        from pegc_sumo.sumo_io import find_binary

        find_binary("sumo")
        find_binary("netconvert")
        return True
    except Exception:
        return False


needs_sumo = pytest.mark.skipif(not _sumo_available(), reason="SUMO is not installed here; run in Codespaces")


@needs_sumo
def test_sumo_batch_is_repeatable_and_parallel_matches_sequential(tmp_path):
    common = ["--scale", "0.7", "--quick", "--densities", "low", "--strategies", "A,C"]
    files = []
    for name, workers in (("seq1", "1"), ("seq2", "1"), ("par", "2")):
        out = tmp_path / f"{name}.csv"
        assert batch.main(common + ["--workers", workers, "--out", str(out)]) == 0
        files.append(out)
    assert files[0].read_bytes() == files[1].read_bytes() == files[2].read_bytes()
    rows = batch.read_csv(str(files[0]))
    assert len(rows) == 4 and all(r["violations"] == 0 for r in rows)
    for a, c in zip(rows[0::2], rows[1::2]):                       # A and C of one scenario saw identical vehicles
        assert (a["strategy"], c["strategy"]) == ("A", "C") and a["routes_sha1"] == c["routes_sha1"] and a["seed"] == c["seed"]


@needs_sumo
def test_sumo_calibration_runs_end_to_end(tmp_path):
    out = tmp_path / "cal.json"
    rc = calibrate.main(["--out", str(out), "--ladder", "0.5,0.7", "--seeds", "201,202", "--workers", "2"])
    cal = json.loads(out.read_text())
    assert rc in (0, 1) and [t["scale"] for t in cal["ladder"]] == [0.5, 0.7]
    assert all(isinstance(t["mean_growth"], float) and t["mean_entered"] > 100 for t in cal["ladder"])
    assert cal["sumo_version"] != "unknown" and cal["status"] in ("ok", "all_passed", "no_scale_passed")

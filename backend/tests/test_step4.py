"""Step 4 tests: Strategy C, C1, C2 (PEGC).

Parity tests replay scripted inputs recorded from the REAL browser engine
(frontend/scripts/export_strategy_c_golden.mjs -> tests/golden_strategy_c.json.gz) through the Python port and
require identical signal states, Strategy C mode machine, log lines (text and data) and violation counts at
every step. The pure tests use synthetic states. The SUMO tests at the bottom skip when SUMO is not installed.
"""
import gzip
import json
import os
from dataclasses import replace

import pytest

from pegc_sumo.ambinfo import Upcoming, eta_free
from pegc_sumo.config import CONFIG, Scenario
from pegc_sumo.network import build_network
from pegc_sumo.runner import RunContext
from pegc_sumo.signals import JunctionSignal, PureSignalBank, other
from pegc_sumo.textfmt import js_fixed
from .test_step3_runner import fake_sumo  # noqa: F401  (fixture: runner driven by a fake TraCI connection)
from pegc_sumo.strategies import (ApproachData, JunctionData, OutData, StrategyC, cross_stats, evaluate,
                                  fixed_req, make_strategy, out_occupancy, pressure, recovery_req, timing)

DT = CONFIG.dt
HERE = os.path.dirname(os.path.abspath(__file__))
PHASE = {"G": 0, "Y": 1, "R": 2}


def _load_golden():
    with gzip.open(os.path.join(HERE, "golden_strategy_c.json.gz"), "rt", encoding="utf-8") as f:
        return json.load(f)


G = _load_golden()


def snap(sig):
    return [PHASE[sig.phase], sig.green_axis, sig.timer, sig.switches, sig.emergency_used]


def cstate(sig):
    return [sig.c_mode, sig.c_recovering, sig.c_last_logged, sig.amb_passed_at]


class FakeTracker:
    def __init__(self):
        self.entries = None

    def upcoming(self):
        return self.entries


def data_from_view(view) -> JunctionData:
    d = JunctionData()
    for axis in "HV":
        d.approach[axis] = [ApproachData(e["queue"], e["queueStreak"], [(v["d"], v["v"], v["streak"]) for v in e["veh"]])
                            for e in view["approach"][axis]]
        d.out[axis] = [OutData(o["length"], list(o["vehLens"])) for o in view["out"][axis]]
    return d


def replay(case):
    """Run one golden case through the Python strategy. Returns (bank, log, per-step mismatches)."""
    net = build_network()
    bank = PureSignalBank(None, net, CONFIG)
    log = []
    bank.event_sink = lambda t, jid, kind, text, data: log.append(
        {"t": t, "junction": jid, "kind": kind, "text": text, "data": data})
    tracker = FakeTracker()
    ctx = RunContext(CONFIG, bank, 0.0, tracker, None,
                     lambda t, jid, kind, text, **data: log.append(
                         {"t": t, "junction": jid, "kind": kind, "text": text, "data": data}))
    strat = make_strategy(case["strategy"])
    ids = list(net.junction_ids)
    tables = [data_from_view(v) for v in case["viewTable"]]
    raw = case["viewTable"]
    problems = []
    for i, (step, exp, cexp) in enumerate(zip(case["steps"], case["states"], case["cstates"])):
        t = i * DT
        ctx.t = t
        ctx.recovery_time = step["recoveryTime"]
        ctx.total_queued = step["totalQueued"]
        by_id = dict(zip(ids, step["views"]))
        ctx.junction_data = lambda jid, by_id=by_id: tables[by_id[jid]]
        bank.queue_by_junction = {j: {ax: sum(e["queue"] for e in raw[by_id[j]]["approach"][ax]) for ax in "HV"} for j in ids}
        tracker.entries = [Upcoming(u["junction"], u["axis"], "x", 0, u["dStop"], u["eta"], -1.0, u["nQueue"])
                           for u in step["upcoming"]]
        bank.update()
        strat.control(ctx)
        bank.check_safety(t)
        got = [snap(bank.signals[j]) for j in ids]
        gotc = [cstate(bank.signals[j]) for j in ids]
        if got != exp:
            problems.append((i, t, "signal states", got, exp))
        elif gotc != cexp:
            problems.append((i, t, "mode machine", gotc, cexp))
        if problems:
            break
    return bank, log, problems


# =====================================================================================
# JS <-> Python parity
# =====================================================================================
def test_golden_config_matches_python_config():
    c = G["config"]
    for js, py in [("dt", "dt"), ("minGreen", "min_green"), ("yellow", "yellow"), ("allRed", "all_red"),
                   ("lockout", "lockout"), ("fixedGreen", "fixed_green"), ("minGap", "min_gap"),
                   ("ambAccel", "amb_accel"), ("ambMaxSpeed", "amb_max_speed"), ("cEtaCruise", "c_eta_cruise"),
                   ("cLookaheadJunctions", "c_lookahead_junctions"), ("cDischargeHeadway", "c_discharge_headway"),
                   ("cMinDelaySaved", "c_min_delay_saved"), ("cWeightAmbDelay", "c_weight_amb_delay"),
                   ("cCostPerCar", "c_cost_per_car"), ("cWaitWeight", "c_wait_weight"),
                   ("cSpillbackPenalty", "c_spillback_penalty"), ("cRecoveryMax", "c_recovery_max")]:
        assert c[js] == getattr(CONFIG, py), js


def test_eta_matches_js_grid():
    for p in G["etaGrid"]:
        assert eta_free(p["v"], p["D"], CONFIG) == p["eta"], p


@pytest.mark.parametrize("case", G["cases"], ids=lambda c: c["name"])
def test_strategy_c_matches_js_step_by_step(case):
    bank, log, problems = replay(case)
    assert not problems, f"{case['name']}: first difference from JS: {problems[0]}"
    assert len(log) == len(case["log"]), f"{case['name']}: {len(log)} log lines, JS has {len(case['log'])}"
    for k, (mine, js) in enumerate(zip(log, case["log"])):
        assert mine == js, f"{case['name']}: log line {k} differs\n python: {mine}\n js:     {js}"
    assert bank.violations == case["final"]["violations"] == 0


def test_golden_cases_cover_every_decision_kind():
    kinds = {e["kind"] for c in G["cases"] for e in c["log"]}
    assert {"hold", "switch", "prepare", "decline", "recovery", "pass", "safety"} <= kinds


# =====================================================================================
# Synthetic states: decisions react to the queue, the cross street, spillback, ablations
# =====================================================================================
def quiet() -> JunctionData:
    d = JunctionData()
    for ax in "HV":
        d.approach[ax] = [ApproachData(0, 0.0, [])]
        d.out[ax] = [OutData(200.0, [])]
    return d


def make_ctx(data=None, recovery_time=None, junction="J00"):
    bank = PureSignalBank(None, build_network(), CONFIG)
    log = []
    bank.event_sink = lambda t, jid, kind, text, d: log.append((jid, kind))
    tracker = FakeTracker()
    ctx = RunContext(CONFIG, bank, 0.0, tracker, None, lambda t, jid, kind, text, **d: log.append((jid, kind, text, d)))
    data = data or quiet()
    ctx.junction_data = lambda jid: data
    ctx.recovery_time = recovery_time
    return ctx, bank, tracker, log, data


def red_for_ambulance(bank, jid="J00", timer=3.0):
    """J00 starts with H green; the ambulance wants V, so V is red (cross street = H)."""
    sig = bank.signals[jid]
    sig.timer = timer
    return sig


def u(jid="J00", axis="V", d_stop=300.0, v=14.0, n=0):
    return Upcoming(jid, axis, "x", 0, d_stop, eta_free(v, d_stop, CONFIG), -1.0, n)


def decide(n, queue_aware=True, cross_cars=0, d_stop=250.0, timer=3.0):
    ctx, bank, tr, log, data = make_ctx()
    data.approach["H"] = [ApproachData(cross_cars, 0.0, [])]
    sig = red_for_ambulance(bank, timer=timer)
    up = u(d_stop=d_stop, n=n)
    req, passed, eta = evaluate(ctx, sig, up, 0, queue_aware)
    return req, sig, log


def test_larger_queue_ahead_means_an_earlier_switch():
    """tStartIn = eta - n*h - tGreenAct: more queued cars ahead of the ambulance -> start sooner."""
    out = []
    for n in (0, 3, 6):
        ctx, bank, tr, log, data = make_ctx()
        sig = red_for_ambulance(bank)
        evaluate(ctx, sig, u(d_stop=250.0, n=n), 0, True)
        out.append([e for e in log if len(e) == 4][0][3]["tStartIn"])
    assert out[0] > out[1] > out[2] == 0
    assert out[0] - out[1] == pytest.approx(3 * CONFIG.c_discharge_headway)


def test_queue_changes_the_decision_from_prepare_to_switch_now():
    _, sig0, log0 = decide(0)
    _, sig6, log6 = decide(6)
    k0 = [e for e in log0 if len(e) == 4][0][1]
    k6 = [e for e in log6 if len(e) == 4][0][1]
    assert (k0, k6) == ("prepare", "switch") and (sig0.c_mode, sig6.c_mode) == ("prepare", "active")


def test_c1_ignores_the_queue_c_uses_it():
    for aware, expect_n_used in ((True, 6), (False, 0)):
        ctx, bank, tr, log, data = make_ctx()
        sig = red_for_ambulance(bank)
        evaluate(ctx, sig, u(d_stop=250.0, n=6), 0, aware)
        entry = [e for e in log if len(e) == 4][0]
        assert entry[3]["nUsed"] == expect_n_used and entry[3]["n"] == 6      # the log always shows the real queue


def test_heavy_cross_traffic_makes_c_decline_and_quiet_cross_street_makes_it_act():
    req_q, sig_q, log_q = decide(3, cross_cars=0, d_stop=150.0)
    req_h, sig_h, log_h = decide(3, cross_cars=60, d_stop=150.0)
    assert req_q.emergency and sig_q.c_mode == "active"
    assert (not req_h.emergency) and sig_h.c_mode == "declined"
    assert [e for e in log_h if len(e) == 4][0][1] == "decline"


def test_spillback_on_the_outgoing_road_raises_the_cost():
    ctx, bank, tr, log, data = make_ctx()
    sig = red_for_ambulance(bank)
    evaluate(ctx, sig, u(d_stop=150.0, n=3), 0, True)
    base_cost = [e for e in log if len(e) == 4][0][3]["cost"]
    ctx2, bank2, _, log2, data2 = make_ctx()
    data2.out["V"] = [OutData(200.0, [4.5] * 40)]
    sig2 = red_for_ambulance(bank2)
    evaluate(ctx2, sig2, u(d_stop=150.0, n=3), 0, True)
    assert [e for e in log2 if len(e) == 4][0][3]["cost"] == pytest.approx(base_cost + 4.0 * 20 * (40 * 6.5 / 200 - 0.5))


def test_cross_stats_counts_queued_and_nearby_moving_cars_but_not_cars_past_the_line():
    d = quiet()
    d.approach["H"] = [ApproachData(2, 0.0, [(10.0, 0.0, 5.0), (30.0, 4.0, 1.0), (60.0, 4.0, 9.0), (-3.0, 0.0, 99.0)])]
    assert cross_stats(d, "H") == (3, 9.0)      # 2 queued + 1 moving within 50 m; longest ignores d < 0


def test_pressure_and_out_occupancy():
    d = quiet()
    d.approach["V"] = [ApproachData(2, 10.0, []), ApproachData(1, 0.0, [])]
    assert pressure(d, "V") == 2 + 0.1 * 10.0 + 1 + 0.0
    d.out["H"] = [OutData(100.0, [4.5, 4.5]), OutData(200.0, [4.5] * 4)]
    assert out_occupancy(d, "H", CONFIG) == max(13.0 / 100.0, 26.0 / 200.0)


def test_timing_cases():
    sig = JunctionSignal.make("J00", 0)           # H green, timer 0
    tm = timing(sig, "H", CONFIG)
    assert tm.green_now and tm.t_end_base == CONFIG.fixed_green
    sig.timer = 3.0
    tm = timing(sig, "V", CONFIG)                 # V is red; H green with 3 s so far: min green 7 s -> 4 s left
    assert not tm.green_now and tm.min_rem == 4.0
    assert tm.t_green_base == (CONFIG.fixed_green - 3.0) + CONFIG.yellow + CONFIG.all_red
    assert tm.t_green_act == 4.0 + CONFIG.yellow + CONFIG.all_red
    sig.phase, sig.timer = "Y", 1.0
    tm = timing(sig, "V", CONFIG)
    assert tm.fixed_seq and tm.t_green_base == tm.t_green_act == CONFIG.yellow - 1.0 + CONFIG.all_red


def test_c_far_from_every_junction_acts_like_a():
    ctx, bank, tr, log, data = make_ctx()
    a_bank = PureSignalBank(None, build_network(), CONFIG)
    strat_c, strat_a = make_strategy("C"), make_strategy("A")
    ctx_a = RunContext(CONFIG, a_bank)
    tr.entries = None
    for i in range(int(200 / DT)):
        ctx.t = ctx_a.t = i * DT
        bank.update(); a_bank.update()
        strat_c.control(ctx); strat_a.control(ctx_a)
        assert [snap(bank.signals[j]) for j in bank.signals] == [snap(a_bank.signals[j]) for j in a_bank.signals]
    assert log == []


def test_ablation_c2_never_enters_recovery_but_c_and_c1_do():
    """Cases 0, 1, 2 of the golden file use identical inputs for C, C1 and C2."""
    for case, expect in ((G["cases"][0], True), (G["cases"][1], True), (G["cases"][2], False)):
        assert any(e["kind"] == "recovery" for e in case["log"]) is expect, case["name"]


def test_c1_and_c_differ_only_through_the_queue_term():
    """Same inputs, same junction: C1 (queue ignored) must log a smaller lead time than C when cars are queued."""
    c, c1 = G["cases"][0], G["cases"][1]
    lead = lambda case: [e["data"]["lead"] for e in case["log"] if e["kind"] in ("hold", "switch", "prepare", "decline")]
    assert lead(c) != lead(c1)


def test_recovery_serves_the_busier_axis_and_ends_when_queues_recover():
    ctx, bank, tr, log, data = make_ctx()
    sig = bank.signals["J00"]
    sig.timer = 8.0
    sig.c_recovering, sig.amb_passed_at = True, 100.0
    data.approach["V"] = [ApproachData(6, 0.0, [])]          # V is busier than H (green)
    ctx.t = 110.0
    req = recovery_req(ctx, sig)
    assert req.desired == "V" and req.rec["po"] == 6 and not req.emergency
    # recovery over once the metric says so
    ctx.recovery_time = 12.0
    StrategyC("C").control(ctx)
    assert sig.c_recovering is False
    assert any(len(e) == 4 and e[1] == "recovery" and "queues are back" in e[2] for e in log)


def test_recovery_times_out_after_the_recovery_window():
    ctx, bank, tr, log, data = make_ctx()
    sig = bank.signals["J00"]
    sig.c_recovering, sig.amb_passed_at = True, 100.0
    ctx.t = 100.0 + CONFIG.c_recovery_max + 1
    StrategyC("C").control(ctx)
    assert sig.c_recovering is False
    assert any(len(e) == 4 and "60 s recovery window ended" in e[2] for e in log)


def test_pass_detection_starts_recovery():
    ctx, bank, tr, log, data = make_ctx()
    strat = StrategyC("C")
    tr.entries = [u("J00", "H", 50.0)]
    ctx.t = 100.0
    strat.control(ctx)
    tr.entries = []                                           # the ambulance has cleared J00
    ctx.t = 101.0
    strat.control(ctx)
    sig = bank.signals["J00"]
    assert sig.c_mode == "passed" and sig.c_recovering and sig.amb_passed_at == 101.0
    assert any(len(e) == 4 and e[1] == "pass" and "J00" in e[2] for e in log)


def test_c_reads_the_queue_lazily_and_only_for_the_first_two_junctions():
    ctx, bank, tr, log, data = make_ctx()
    calls = []
    ctx.queue_ahead = lambda up: calls.append(up.junction) or 0
    tr.entries = [Upcoming(j, "H", "x", 0, 300.0 + 220 * k, 25.0, -1.0, None) for k, j in enumerate(["J00", "J01", "J02"])]
    StrategyC("C").control(ctx)
    assert calls == ["J00", "J01"]                            # lookahead is 2 junctions


def test_c_requests_only_through_the_safety_layer():
    """Strategy C never touches the signal state itself: only bank.request may start a switch."""
    ctx, bank, tr, log, data = make_ctx()
    sig = bank.signals["J00"]
    sig.timer = 8.0
    tr.entries = [u("J00", "V", 60.0, n=0)]
    before = sig.switches
    StrategyC("C").control(ctx)
    assert sig.switches == before + 1 and sig.phase == "Y"      # via apply_request (min green was over)
    assert bank.request_stats["switched"] == 1


# =====================================================================================
# The real runner loop (junction data, recovery signal, log sink) against a fake TraCI connection
# =====================================================================================
@pytest.mark.parametrize("sid", ["C", "C1", "C2"])
def test_runner_c_family_with_fake_sumo(fake_sumo, sid):
    from pegc_sumo import runner
    r = runner.run_scenario(Scenario(seed=7, density="low"), sid, label=f"fake_{sid}")
    assert r.metrics["ambFinished"] and r.metrics["violations"] == 0
    assert r.strategy == sid
    decisions = [e for e in r.log if e["kind"] in ("hold", "switch", "prepare", "decline")]
    assert decisions, "C never evaluated a junction"
    for e in decisions:                                  # text is generated from the logged values
        assert f"ETA {js_fixed(e['data']['eta'])} s" in e["text"]
        assert f"queue ahead {e['data']['n']}" in e["text"]
    passes = [e for e in r.log if e["kind"] == "pass" and "data" in e and "at" in e["data"]]
    assert len(passes) >= 3                              # every junction on the route is reported when cleared
    has_recovery = any(e["kind"] == "recovery" for e in r.log)
    assert has_recovery is (sid != "C2")


# =====================================================================================
# Real SUMO runs (skipped when SUMO is not installed; they run in Codespaces)
# =====================================================================================
def _have_sumo():
    try:
        from pegc_sumo.sumo_io import find_binary
        find_binary("sumo")
        find_binary("netconvert")
        import traci  # noqa: F401
        return True
    except Exception:
        return False


needs_sumo = pytest.mark.skipif(not _have_sumo(), reason="SUMO is not installed")


@needs_sumo
@pytest.mark.parametrize("sid", ["C", "C1", "C2"])
def test_sumo_run_has_zero_violations(sid):
    from pegc_sumo.runner import run_scenario
    r = run_scenario(Scenario(seed=1, density="medium"), sid, label=f"s4_{sid}")
    assert r.safety["violations"] == 0, r.safety
    assert r.metrics["ambFinished"]


@needs_sumo
def test_sumo_c_log_lines_are_generated_from_real_values():
    from pegc_sumo.runner import run_scenario
    r = run_scenario(Scenario(seed=1, density="medium"), "C", label="s4_log")
    kinds = {e["kind"] for e in r.log}
    assert kinds & {"hold", "switch", "prepare", "decline"}
    for e in r.log:
        if e["kind"] in ("hold", "switch", "prepare", "decline"):
            assert f"ETA {js_fixed(e['data']['eta'])} s" in e["text"] and f"queue ahead {e['data']['n']}" in e["text"]
    c2 = run_scenario(Scenario(seed=1, density="medium"), "C2", label="s4_c2")
    assert not any(e["kind"] == "recovery" for e in c2.log)


@needs_sumo
def test_sumo_c_and_a_share_traffic_until_the_ambulance_appears():
    from pegc_sumo.runner import run_scenario
    a = run_scenario(Scenario(seed=2, density="low"), "A", label="s4_a")
    c = run_scenario(Scenario(seed=2, density="low"), "C", label="s4_c")
    assert [s for s in a.series if s[0] <= CONFIG.warmup] == [s for s in c.series if s[0] <= CONFIG.warmup]

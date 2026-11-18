"""Step 3 tests: safety layer, Strategy B, JS-Python parity.

Most tests are pure Python (no SUMO). The SUMO tests at the bottom skip themselves when SUMO is not
installed, and run in Codespaces. Set PEGC_FULL=1 to run the full 10-seed x 3-density violation matrix.

The parity tests replay scripted inputs recorded from the REAL browser engine
(frontend/scripts/export_safety_golden.mjs -> tests/golden_safety.json.gz) through the Python code and
require identical signal states, results, log lines and violation counts at every step.
"""
import gzip
import json
import os
import random
from dataclasses import replace

import pytest

from pegc_sumo.ambinfo import AmbState, AmbTracker, Upcoming, eta_free
from pegc_sumo.config import CONFIG, Scenario
from pegc_sumo.network import build_network, find_route, sumo_edge_id
from pegc_sumo.runner import RunContext
from pegc_sumo.safety import RULES, Request, SafetyCounter, apply_request, check_safety
from pegc_sumo.signals import JunctionSignal, PureSignalBank, other
from pegc_sumo.strategies import StrategyA, StrategyB, fixed_req, make_strategy
from pegc_sumo.textfmt import js_fixed

DT = CONFIG.dt
HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN_PATH = os.path.join(HERE, "golden_safety.json.gz")
RULE_MAP = {"conflict": "conflict", "minGreen": "min_green", "yellow": "yellow", "allRed": "all_red",
            "holdCap": "hold_cap", "maxWait": "max_wait"}
PHASE = {"G": 0, "Y": 1, "R": 2}


def _load_golden():
    with gzip.open(GOLDEN_PATH, "rt", encoding="utf-8") as f:
        return json.load(f)


G = _load_golden()


def snap(sig):
    """Same layout as snap() in export_safety_golden.mjs."""
    return [PHASE[sig.phase], sig.green_axis, sig.timer, sig.switches, sig.emergency_used]


def fresh(timer: float = 0.0, junction: str = "J00", idx: int = 0) -> JunctionSignal:
    sig = JunctionSignal.make(junction, idx)
    sig.timer = timer
    return sig


# =====================================================================================
# JS <-> Python parity (the strongest check that the port is faithful)
# =====================================================================================
def test_golden_config_matches_python_config():
    c = G["config"]
    assert c["dt"] == CONFIG.dt and c["minGreen"] == CONFIG.min_green and c["yellow"] == CONFIG.yellow
    assert c["allRed"] == CONFIG.all_red and c["maxEmergencyHold"] == CONFIG.max_emergency_hold
    assert c["maxWait"] == CONFIG.max_wait and c["lockout"] == CONFIG.lockout and c["fixedGreen"] == CONFIG.fixed_green
    assert c["bTriggerDistance"] == CONFIG.b_trigger_distance
    assert c["ambAccel"] == CONFIG.amb_accel and c["ambMaxSpeed"] == CONFIG.amb_max_speed
    assert c["cEtaCruise"] == CONFIG.c_eta_cruise


@pytest.mark.parametrize("case", G["signalCases"], ids=lambda c: c["name"])
def test_safety_layer_matches_js_step_by_step(case):
    r, c = case["junction"]["r"], case["junction"]["c"]
    sig = JunctionSignal.make(f"J{r}{c}", r * CONFIG.cols + c)
    counter = SafetyCounter()
    for i, (step, exp) in enumerate(zip(case["steps"], case["expected"])):
        t = i * DT
        sig.update()
        qd = step["queue"]

        def aq(_jid, axis, qd=qd):
            return qd[axis]

        op, result = step["op"], None
        if op and op["type"] == "request":
            res = apply_request(sig, Request(op["desired"], op["emergency"], op["baselineDesired"]), t, CONFIG, aq)
            result = {"switched": res.switched, "delayed": res.delayed, "delayRemaining": res.delay_remaining,
                      "events": [{"kind": e["kind"], "text": e["text"], "rule": e["data"]["rule"]} for e in res.events]}
        elif op and op["type"] == "force_switch":
            sig.start_switch(t)
        elif op and op["type"] == "set_emergency_used":
            sig.emergency_used = op["value"]
        check_safety([sig], t, CONFIG, counter, aq)
        assert snap(sig) == exp["state"], f"{case['name']}: signal state differs from JS at step {i} (t={t})"
        assert result == exp["result"], f"{case['name']}: request result differs from JS at step {i} (t={t})"
        assert counter.violations == exp["violations"], f"{case['name']}: violation count differs at step {i}"
    final = case["final"]
    assert counter.violations == final["violations"]
    for js_rule, n in final["byRule"].items():
        assert counter.by_rule[RULE_MAP[js_rule]] == n
    assert sum(counter.by_rule.values()) == final["violations"]
    assert sig.last_green_end == final["lastGreenEnd"]


class FakeTracker:
    """Stands in for AmbTracker: returns whatever list of Upcoming entries the test sets."""

    def __init__(self):
        self.entries = None

    def upcoming(self):
        return self.entries


@pytest.mark.parametrize("case", G["bCases"], ids=lambda c: c["name"])
def test_strategy_b_matches_js_step_by_step(case):
    net = build_network()
    bank = PureSignalBank(None, net, CONFIG)
    bank.queue_by_junction = {j: {"H": 2, "V": 2} for j in bank.signals}   # same queues as the JS script
    log = []
    bank.event_sink = lambda t, jid, kind, text, data: log.append(
        {"t": t, "junction": jid, "kind": kind, "text": text, "data": data})
    ctx = RunContext(CONFIG, bank, 0.0, FakeTracker(), None,
                     lambda t, jid, kind, text, **data: log.append(
                         {"t": t, "junction": jid, "kind": kind, "text": text, "data": data}))
    strat = StrategyB()
    for i, (step, exp) in enumerate(zip(case["steps"], case["states"])):
        t = i * DT
        ctx.t = t
        ctx.amb.entries = [Upcoming(u["junction"], u["axis"], "x", 0, u["dStop"], u["eta"], -1.0, u["nQueue"])
                           for u in step["upcoming"]]
        bank.update()
        strat.control(ctx)
        bank.check_safety(t)
        got = [snap(bank.signals[j]) for j in net.junction_ids]
        assert got == exp, f"{case['name']}: signal states differ from JS at step {i} (t={t})"
    assert len(log) == len(case["log"]), f"{case['name']}: {len(log)} log lines, JS has {len(case['log'])}"
    for k, (mine, js) in enumerate(zip(log, case["log"])):
        assert mine == js, f"{case['name']}: log line {k} differs\n python: {mine}\n js:     {js}"
    assert bank.violations == case["final"]["violations"]
    for js_rule, n in case["final"]["byRule"].items():
        assert bank.by_rule[RULE_MAP[js_rule]] == n


def test_eta_free_matches_js():
    assert len(G["etaGrid"]) >= 80
    for row in G["etaGrid"]:
        assert eta_free(row["v"], row["D"], CONFIG) == pytest.approx(row["eta"], rel=1e-12, abs=1e-12), row


def test_js_fixed_matches_javascript_rounding():
    # JavaScript's toFixed rounds an exact tie upward; Python's format() rounds it to even.
    assert js_fixed(0.125, 2) == "0.13" and f"{0.125:.2f}" == "0.12"
    assert js_fixed(86.5, 0) == "87" and js_fixed(2.5, 0) == "3" and js_fixed(6.75, 1) == "6.8"
    assert js_fixed(8.65, 1) == "8.7" and js_fixed(84, 0) == "84" and js_fixed(118.0, 0) == "118"


# =====================================================================================
# Every safety rule, with synthetic inputs
# =====================================================================================
def test_minimum_green_delays_a_switch_and_logs_it_once():
    sig = fresh(3.0)
    res = apply_request(sig, Request("V", True, "H"), 10.0, CONFIG)
    assert (res.outcome, res.reason, res.delayed, res.switched) == ("delayed", "min_green", True, False)
    assert res.delay_remaining == pytest.approx(4.0) and sig.phase == "G" and sig.switches == 0
    assert len(res.events) == 1 and "minimum green 7 s" in res.events[0]["text"] and res.events[0]["data"]["rule"] == "minGreen"
    assert apply_request(sig, Request("V", True, "H"), 10.25, CONFIG).events == []       # logged once per green
    sig.timer = 7.0
    res = apply_request(sig, Request("V", True, "H"), 14.0, CONFIG)
    assert res.outcome == "switched" and res.switched and sig.phase == "Y" and sig.next_axis == "V"
    assert sig.last_switch_green_dur == 7.0 and sig.last_green_end["H"] == 14.0


def test_a_normal_request_is_delayed_silently():
    res = apply_request(fresh(2.0), Request("V", False, "V"), 5.0, CONFIG)
    assert res.outcome == "delayed" and res.events == []


def test_lockout_applies_when_it_is_longer_than_minimum_green():
    cfg = replace(CONFIG, lockout=10.0)
    sig = fresh(8.0)
    res = apply_request(sig, Request("V", False, "V"), 20.0, cfg)
    assert res.outcome == "delayed" and res.delay_remaining == pytest.approx(2.0)
    sig.timer = 10.0
    assert apply_request(sig, Request("V", False, "V"), 22.0, cfg).outcome == "switched"


def test_lockout_shorter_than_minimum_green_changes_nothing():
    cfg = replace(CONFIG, lockout=2.0)
    sig = fresh(6.75)
    assert apply_request(sig, Request("V", False, "V"), 20.0, cfg).outcome == "delayed"
    sig.timer = 7.0
    assert apply_request(sig, Request("V", False, "V"), 20.25, cfg).outcome == "switched"


def test_emergency_hold_cap_releases_after_25_seconds():
    sig = fresh(10.0)
    req = Request("H", True, "V")                      # hold H green against a plan that wants V
    for n in range(100):                               # 100 steps x 0.25 s = 25 s
        res = apply_request(sig, req, n * DT, CONFIG)
        assert res.outcome == "accepted" and res.reason is None
    assert sig.emergency_used == pytest.approx(25.0)
    res = apply_request(sig, req, 25.0, CONFIG)        # cap reached: falls back to the normal plan (V)
    assert res.reason == "hold_cap" and res.outcome == "switched" and sig.phase == "Y"
    assert res.events[0]["text"] == "Safety layer: emergency hold at J00 reached the 25 s cap; releasing to normal plan."
    assert sig.emergency_used == pytest.approx(25.0)   # never goes above the cap


def test_hold_cap_only_counts_when_holding_against_the_plan():
    sig = fresh(10.0)
    for n in range(200):
        apply_request(sig, Request("H", True, "H"), n * DT, CONFIG)   # plan agrees with the emergency
    assert sig.emergency_used == 0.0
    for n in range(50):
        apply_request(sig, Request("H", False, "V"), n * DT, CONFIG)  # not an emergency request
    assert sig.emergency_used == 0.0


def test_max_wait_forces_a_switch_and_overrides_emergency():
    def queued(_j, axis):
        return 4 if axis == "V" else 0

    sig = fresh(30.0)                                   # H green for a long time; V last had green 5 s before t=0
    assert sig.last_green_end["V"] == -5.0
    res = apply_request(sig, Request("H", True, "H"), 78.75, CONFIG, queued)   # waited 83.75 s < 84 s limit
    assert res.outcome == "accepted"
    res = apply_request(sig, Request("H", True, "H"), 79.0, CONFIG, queued)    # waited 84 s
    assert res.outcome == "switched" and res.reason == "max_wait" and sig.phase == "Y"
    assert "V-axis approach at J00 has waited 84 s; forcing a switch (overrides emergency requests)." in res.events[0]["text"]


def test_max_wait_needs_a_queue_and_minimum_green():
    sig = fresh(30.0)
    assert apply_request(sig, Request("H", True, "H"), 100.0, CONFIG, lambda j, a: 0).outcome == "accepted"   # nobody waiting
    sig = fresh(5.0)
    assert apply_request(sig, Request("H", True, "H"), 100.0, CONFIG, lambda j, a: 3).outcome == "accepted"   # green too young


def test_requests_during_yellow_and_all_red_are_rejected():
    for phase in ("Y", "R"):
        sig = fresh(1.0)
        sig.phase = phase
        used = sig.emergency_used
        res = apply_request(sig, Request("H", True, "V"), 50.0, CONFIG)
        assert (res.outcome, res.reason, res.switched, res.delayed, res.events) == ("rejected", "transition", False, False, [])
        assert sig.phase == phase and sig.emergency_used == used and sig.switches == 0


def test_nothing_to_change_is_accepted():
    res = apply_request(fresh(12.0), Request("H"), 12.0, CONFIG)
    assert (res.outcome, res.reason, res.switched, res.delayed, res.events) == ("accepted", None, False, False, [])


def test_signal_bank_request_goes_through_the_safety_layer():
    bank = PureSignalBank(None, build_network(), CONFIG)
    seen = []
    bank.event_sink = lambda t, jid, kind, text, data: seen.append((t, jid, kind))
    bank.signals["J00"].timer = 3.0
    assert bank.request("J00", "V", 5.0).outcome == "delayed"                    # a plain axis letter is accepted
    assert bank.request("J00", Request("V", True, "H"), 5.25).outcome == "delayed"
    assert seen == [(5.25, "J00", "safety")] and len(bank.events) == 1
    bank.signals["J00"].timer = 7.0
    assert bank.request("J00", Request("V", True, "H"), 9.0).switched
    assert bank.switch_log == [(9.0, "J00", "V")]
    assert bank.request("J00", "H", 9.25).outcome == "rejected"
    assert bank.request_stats == {"accepted": 0, "switched": 1, "delayed": 2, "rejected": 1}


# =====================================================================================
# The independent checker
# =====================================================================================
def test_checker_flags_each_violation_type():
    sig = fresh(12.0)
    counter = SafetyCounter()
    check_safety([sig], 0.0, CONFIG, counter)
    assert counter.violations == 0

    sig.last_switch_green_dur = 3.0
    check_safety([sig], 1.0, CONFIG, counter)
    assert counter.by_rule["min_green"] == 1 and sig.last_switch_green_dur is None   # one-shot: consumed
    sig.last_yellow_dur = 2.0
    check_safety([sig], 2.0, CONFIG, counter)
    assert counter.by_rule["yellow"] == 1
    sig.last_red_dur = 1.0
    check_safety([sig], 3.0, CONFIG, counter)
    assert counter.by_rule["all_red"] == 1
    sig.emergency_used = 40.0
    check_safety([sig], 4.0, CONFIG, counter)
    assert counter.by_rule["hold_cap"] == 1
    sig.emergency_used = 0.0
    sig.last_yellow_dur, sig.last_red_dur, sig.last_switch_green_dur = 3.0, 2.0, 7.0   # correct durations: fine
    check_safety([sig], 5.0, CONFIG, counter)
    assert counter.violations == 4

    sig.light_for = lambda axis: "G"                                                   # two axes green at once
    check_safety([sig], 6.0, CONFIG, counter)
    assert counter.by_rule["conflict"] == 1 and counter.violations == 5
    assert [d["rule"] for d in counter.details] == ["min_green", "yellow", "all_red", "hold_cap", "conflict"]


def test_checker_counts_a_max_wait_breach_once_per_wait():
    sig = fresh(12.0)
    sig.last_green_end["V"] = 0.0
    counter = SafetyCounter()
    queued = lambda j, a: 2   # noqa: E731
    check_safety([sig], 90.0, CONFIG, counter, queued)
    assert counter.violations == 0                                  # exactly 90 s is not above the limit
    check_safety([sig], 91.0, CONFIG, counter, queued)
    check_safety([sig], 92.0, CONFIG, counter, queued)
    assert counter.by_rule["max_wait"] == 1                         # latched
    sig.last_green_end["V"] = 95.0
    sig.wait_latch["V"] = False                                     # what the signal does when V becomes green again
    check_safety([sig], 190.0, CONFIG, counter, queued)
    assert counter.by_rule["max_wait"] == 2
    check_safety([sig], 300.0, CONFIG, SafetyCounter(), lambda j, a: 0)   # no queue: no breach (separate counter)


def test_all_rule_names_are_counted_by_the_bank():
    bank = PureSignalBank(None, build_network(), CONFIG)
    assert set(RULES) <= set(bank.by_rule)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_requests_never_cause_a_violation(seed):
    """Fixed, emergency and random requests, flickering queues: the safety layer must keep violations at 0."""
    rnd = random.Random(seed)
    bank = PureSignalBank(None, build_network(), CONFIG)
    for i in range(int(600 / DT)):
        t = i * DT
        bank.update()
        bank.queue_by_junction = {j: {"H": rnd.randint(0, 3), "V": rnd.randint(0, 3)} for j in bank.signals}
        for jid, sig in bank.signals.items():
            r = rnd.random()
            if r < 0.35:
                req = Request(rnd.choice("HV"), True, rnd.choice("HV"))
            elif r < 0.6:
                req = Request(rnd.choice("HV"), False)
            else:
                req = fixed_req(sig, CONFIG)
            bank.request(jid, req, t)
        bank.check_safety(t)
        for sig in bank.signals.values():
            assert not (sig.light_for("H") != "R" and sig.light_for("V") != "R")
    assert bank.violations == 0, bank.by_rule
    assert bank.request_stats["switched"] > 50 and bank.request_stats["delayed"] > 0 and bank.request_stats["rejected"] > 0


# =====================================================================================
# Strategy A is unchanged by the safety layer
# =====================================================================================
def test_strategy_a_plan_is_identical_to_the_step2_placeholder_rule():
    net = build_network()
    new = PureSignalBank(None, net, CONFIG)
    ctx = RunContext(CONFIG, new)
    strat = StrategyA()
    ref = {jid: JunctionSignal.make(jid, i, CONFIG) for i, jid in enumerate(net.junction_ids)}
    for i in range(int(CONFIG.max_sim_time / DT)):
        ctx.t = i * DT
        new.update()
        strat.control(ctx)
        new.check_safety(ctx.t)
        for jid, sig in ref.items():                     # the Step 2 rule, verbatim
            sig.update(CONFIG)
            want = other(sig.green_axis) if sig.timer >= CONFIG.fixed_green else sig.green_axis
            if not (want == sig.green_axis or sig.phase != "G" or sig.timer + 1e-9 < CONFIG.min_green):
                sig.start_switch()
            n = new.signals[jid]
            assert (n.phase, n.green_axis, n.next_axis, n.timer, n.switches) == \
                   (sig.phase, sig.green_axis, sig.next_axis, sig.timer, sig.switches), (jid, ctx.t)
    assert new.violations == 0 and new.events == []      # A never produces a safety event


# =====================================================================================
# Strategy B with synthetic ambulance states
# =====================================================================================
def make_b():
    bank = PureSignalBank(None, build_network(), CONFIG)
    tracker = FakeTracker()
    log = []
    ctx = RunContext(CONFIG, bank, 0.0, tracker, None, lambda t, j, k, x, **d: log.append((t, j, k, x, d)))
    return bank, tracker, ctx, log


def up(junction, d, axis="H", nq=0, speed=10.0):
    return Upcoming(junction, axis, "x_" + junction, 0, d, eta_free(speed, d, CONFIG), -1.0, nq)


def test_b_triggers_at_120_m_and_not_at_121():
    bank, tr, ctx, log = make_b()
    tr.entries = [up("J00", 120.25)]
    StrategyB().control(ctx)
    assert not bank.signals["J00"].b_active and log == []
    tr.entries = [up("J00", 120.0, nq=3)]
    StrategyB().control(ctx)
    assert bank.signals["J00"].b_active and len(log) == 1
    t, jid, kind, text, data = log[0]
    assert (jid, kind) == ("J00", "request") and data["nQueue"] == 3 and data["dStop"] == 120.0
    assert text.startswith("J00: ambulance 120 m from the stop line, ETA ") and "queue ahead 3 → requesting H-axis green now" in text


def test_b_request_is_an_emergency_request_for_the_ambulance_axis_only_at_its_junction():
    bank, tr, ctx, _ = make_b()
    calls = []
    orig = bank.request
    bank.request = lambda jid, req, t=0.0: (calls.append((jid, req)), orig(jid, req, t))[1]
    tr.entries = [up("J01", 80.0, axis="H")]
    StrategyB().control(ctx)
    by = dict(calls)
    assert len(calls) == 6
    assert by["J01"].desired == "H" and by["J01"].emergency is True and by["J01"].baseline == fixed_req(bank.signals["J01"], CONFIG).desired
    for jid in ("J00", "J02", "J10", "J11", "J12"):
        assert by[jid].emergency is False and by[jid].desired == fixed_req(bank.signals[jid], CONFIG).desired


def test_b_only_looks_at_the_first_upcoming_junction():
    bank, tr, ctx, log = make_b()
    tr.entries = [up("J00", 50.0), up("J01", 60.0)]       # both inside 120 m: only the first counts
    StrategyB().control(ctx)
    assert bank.signals["J00"].b_active and not bank.signals["J01"].b_active
    assert [e[1] for e in log] == ["J00"]


def test_b_releases_once_after_the_ambulance_has_passed():
    bank, tr, ctx, log = make_b()
    strat = StrategyB()
    tr.entries = [up("J00", 40.0)]
    strat.control(ctx)
    tr.entries = [up("J01", 200.0)]                       # J00 passed, next junction far away
    strat.control(ctx)
    strat.control(ctx)
    assert [e[2] for e in log] == ["request", "release"]
    assert log[1][3] == "J00: ambulance request released; back to the fixed plan." and not bank.signals["J00"].b_active
    tr.entries = None                                     # ambulance finished
    strat.control(ctx)
    assert [e[2] for e in log] == ["request", "release"]


def test_b_reads_the_queue_ahead_lazily_only_when_it_first_requests():
    bank, tr, ctx, log = make_b()
    asked = []
    ctx.queue_ahead = lambda u: (asked.append(u.junction), 5)[1]
    tr.entries = [up("J00", 100.0, nq=None)]
    strat = StrategyB()
    strat.control(ctx)
    strat.control(ctx)
    assert asked == ["J00"] and log[0][4]["nQueue"] == 5 and "queue ahead 5" in log[0][3]


def test_b_is_still_limited_by_the_safety_layer():
    bank, tr, ctx, log = make_b()
    strat = StrategyB()
    tr.entries = [up("J00", 100.0, axis="V")]             # J00 starts green H with 0 s of green: V must wait
    phases = []
    for i in range(80):
        ctx.t = i * DT
        bank.update()
        strat.control(ctx)
        bank.check_safety(ctx.t)
        phases.append(bank.signals["J00"].phase)
    sig = bank.signals["J00"]
    assert bank.request_stats["delayed"] > 0 and sig.switches >= 1
    first_switch = phases.index("Y")
    assert first_switch * DT >= CONFIG.min_green - DT     # not before minimum green
    assert bank.violations == 0
    assert any(e[2] == "safety" and e[4]["rule"] == "minGreen" for e in bank.events)


def test_b_behaves_exactly_like_a_without_an_ambulance():
    net = build_network()
    a, b = PureSignalBank(None, net, CONFIG), PureSignalBank(None, net, CONFIG)
    ctx_a, ctx_b = RunContext(CONFIG, a), RunContext(CONFIG, b, 0.0, FakeTracker(), None, None)
    sa, sb = StrategyA(), StrategyB()
    for i in range(int(CONFIG.max_sim_time / DT)):
        for ctx, bank, strat in ((ctx_a, a, sa), (ctx_b, b, sb)):
            ctx.t = i * DT
            bank.update()
            strat.control(ctx)
        assert [snap(a.signals[j]) for j in a.signals] == [snap(b.signals[j]) for j in b.signals]
    assert a.switch_log == b.switch_log and a.violations == b.violations == 0


def test_make_strategy_b_ready_and_c_not():
    assert make_strategy("B").id == "B"
    for sid in ("C", "C1", "C2"):
        with pytest.raises(NotImplementedError):
            make_strategy(sid)


# =====================================================================================
# Ambulance tracker (distances to stop lines)
# =====================================================================================
def route_and_lengths():
    net = build_network()
    route = find_route(net, "W0", "E1")
    ids = [e.id for e in route]
    lengths = {sumo_edge_id(e.id): (172.8 if net.nodes[e.frm].type == "boundary" or net.nodes[e.to].type == "boundary" else 205.6)
               for e in route}
    return net, route, ids, lengths


def test_tracker_distances_axes_and_junction_passing():
    net, route, ids, lengths = route_and_lengths()
    tr = AmbTracker(net, ids, CONFIG, lengths)
    assert tr.upcoming() is None                                   # not in the network yet
    L = [lengths[sumo_edge_id(i)] for i in ids]
    enters = [k for k, e in enumerate(route) if net.nodes[e.to].type == "junction"]
    tr.update(AmbState(sumo_edge_id(ids[0]), 0.0, 10.0))
    up0 = tr.upcoming()
    assert [u.junction for u in up0] == [route[k].to for k in enters]
    assert up0[0].d_stop == pytest.approx(L[0]) and up0[0].k == 0 and up0[0].from_pos == 0.0
    assert up0[0].axis == "H" and up0[0].eta == pytest.approx(eta_free(10.0, L[0], CONFIG))
    for u, k in zip(up0, enters):                                  # cumulative distance to every stop line ahead
        assert u.d_stop == pytest.approx(sum(L[: k + 1]))
        e = route[k]
        assert u.axis == ("H" if net.nodes[e.frm].y == net.nodes[e.to].y else "V")
        assert u.edge == sumo_edge_id(e.id)
    tr.update(AmbState(sumo_edge_id(ids[0]), 100.0, 14.0))
    assert tr.upcoming()[0].d_stop == pytest.approx(L[0] - 100.0)
    # inside the first junction: that junction is passed, the next one is measured from the start of its approach
    tr.update(AmbState(":J00_3_0", 4.0, 8.0))
    inside = tr.upcoming()
    assert inside[0].junction == route[enters[1]].to and inside[0].d_stop == pytest.approx(sum(L[1: enters[1] + 1]))
    assert inside[0].from_pos == -1.0 and len(inside) == len(enters) - 1
    # on the final edge to the exit there is no junction ahead
    tr.update(AmbState(sumo_edge_id(ids[-1]), 10.0, 14.0))
    assert tr.upcoming() == []
    tr.update(None)                                                # reached its destination
    assert tr.upcoming() is None
    tr.update(AmbState("somewhere_else", 5.0, 5.0))                # not on the route: no information
    assert tr.upcoming() is None


def test_tracker_distance_decreases_as_the_ambulance_moves():
    net, route, ids, lengths = route_and_lengths()
    tr = AmbTracker(net, ids, CONFIG, lengths)
    last = None
    for pos in range(0, 170, 5):
        tr.update(AmbState(sumo_edge_id(ids[0]), float(pos), 12.0))
        d = tr.upcoming()[0].d_stop
        assert last is None or d < last
        last = d


# =====================================================================================
# SUMO tests: skipped when SUMO is not installed. Run them in Codespaces.
# =====================================================================================
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


needs_sumo = pytest.mark.skipif(not _sumo_available(), reason="SUMO (eclipse-sumo, traci, sumolib) is not installed here; run in Codespaces")


@pytest.fixture(scope="module")
def b7():
    from pegc_sumo.runner import run_scenario
    return run_scenario(Scenario(seed=7, density="medium"), "B", label="t3_b7")


@needs_sumo
def test_sumo_strategy_b_run_is_clean_and_explains_itself(b7):
    m = b7.metrics
    assert m["ambFinished"] and 60 < m["ambTravelTime"] < 400
    assert m["violations"] == 0 and all(n == 0 for n in b7.safety["byRule"].values())
    kinds = [e["kind"] for e in b7.log]
    assert kinds[0] == "enter" and kinds[-1] == "arrive" and "pass" in kinds
    assert "request" in kinds and "release" in kinds
    for e in b7.log:
        if e["kind"] == "request":
            assert "requesting" in e["text"] and isinstance(e["data"]["nQueue"], int) and e["data"]["nQueue"] >= 0
            assert 100.0 <= e["data"]["dStop"] <= CONFIG.b_trigger_distance
            assert e["data"]["junction"] in e["text"]
    assert b7.safety["requests"]["switched"] > 0


@needs_sumo
def test_sumo_a_and_b_share_traffic_until_the_ambulance_appears_then_differ():
    from pegc_sumo.runner import run_scenario
    differs = 0
    for seed in (1, 2, 3):
        a = run_scenario(Scenario(seed=seed, density="medium"), "A", label=f"t3_a{seed}")
        b = run_scenario(Scenario(seed=seed, density="medium"), "B", label=f"t3_b{seed}")
        pre_a = [p for p in a.series if p[0] <= CONFIG.warmup]
        pre_b = [p for p in b.series if p[0] <= CONFIG.warmup]
        assert pre_a and pre_a == pre_b                           # identical traffic and signals before the ambulance
        assert a.metrics["recoveryBaseline"] == b.metrics["recoveryBaseline"]
        assert a.routes_sha1 == b.routes_sha1                     # same vehicles and ambulance route
        if a.metrics["ambTravelTime"] != b.metrics["ambTravelTime"]:
            differs += 1
        assert a.metrics["violations"] == b.metrics["violations"] == 0
    assert differs >= 1                                           # the strategies give different ambulance outcomes


@needs_sumo
def test_sumo_zero_violations_matrix():
    from pegc_sumo.runner import run_scenario
    seeds = range(1, 11) if os.environ.get("PEGC_FULL") else range(1, 4)
    bad = []
    n = 0
    for strategy in ("A", "B"):
        for density in ("low", "medium", "high"):
            for seed in seeds:
                r = run_scenario(Scenario(seed=seed, density=density), strategy, label=f"t3_m_{strategy}{density}{seed}")
                n += 1
                if r.metrics["violations"] != 0 or any(r.safety["byRule"].values()):
                    bad.append((strategy, density, seed, r.safety["byRule"]))
    assert not bad, f"{len(bad)} of {n} runs had violations: {bad[:5]}"


@needs_sumo
def test_sumo_tracker_distance_agrees_with_sumo_next_tls():
    """The tracker's distance to the next stop line must agree with SUMO's own getNextTLS distance."""
    from pegc_sumo.sumo_io import build_scenario_files, build_static_network, start_sumo

    net, path = build_static_network()
    files = build_scenario_files(net, Scenario(seed=7, density="low"))
    amb = files["amb"]
    conn = start_sumo(path, files["routes"], 7, "t3_tracker")
    try:
        lengths = {sumo_edge_id(e): conn.lane.getLength(sumo_edge_id(e) + "_0") for e in amb.edges}
        tr = AmbTracker(net, amb.edges, CONFIG, lengths)
        checked, worst = 0, 0.0
        for k in range(int(300 / DT)):
            conn.simulationStep()
            if amb.id not in conn.vehicle.getIDList():
                if checked:
                    break
                continue
            road = conn.vehicle.getRoadID(amb.id)
            tr.update(AmbState(road, conn.vehicle.getLanePosition(amb.id), conn.vehicle.getSpeed(amb.id)))
            if road.startswith(":") or k % 8:
                continue
            nxt, up = conn.vehicle.getNextTLS(amb.id), tr.upcoming()
            if nxt and up:
                assert nxt[0][0] == up[0].junction
                worst = max(worst, abs(nxt[0][2] - up[0].d_stop))
                checked += 1
        assert checked > 15
        assert worst < 2.0, f"tracker and SUMO disagree by up to {worst:.2f} m"
    finally:
        conn.close()

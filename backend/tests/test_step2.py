"""Step 2 tests: signal state machine, TraCI link mapping, metrics recorder, runner."""
import json
import os

import pytest

from pegc_sumo.config import CONFIG, Scenario
from pegc_sumo.metrics import MetricsRecorder
from pegc_sumo.network import build_network
from pegc_sumo.runner import run_scenario
from pegc_sumo.signals import JunctionSignal, SignalBank, other
from pegc_sumo.strategies import make_strategy
from pegc_sumo.sumo_io import build_scenario_files, build_static_network, start_sumo

DT = CONFIG.dt


# ---------------- pure Python: the signal plan ----------------
def test_initial_signal_offsets_match_browser_engine():
    net = build_network()
    got = [(j, s.green_axis, s.timer) for j, s in
           ((jid, JunctionSignal.make(jid, i)) for i, jid in enumerate(net.junction_ids))]
    assert got == [("J00", "H", 0), ("J01", "V", 7), ("J02", "H", 14),
                   ("J10", "V", 1), ("J11", "H", 8), ("J12", "V", 15)]


def test_fixed_plan_cycle_durations():
    sig = JunctionSignal.make("J00", 0)
    durations = {"G": [], "Y": [], "R": []}
    run_len, last = 0.0, sig.phase
    for _ in range(int(400 / DT)):
        changed = sig.update()
        if sig.phase == "G" and sig.timer >= CONFIG.fixed_green and sig.timer >= CONFIG.min_green:
            sig.start_switch()
            changed = True
        run_len += DT
        if changed or sig.phase != last:
            durations[last].append(run_len)
            run_len, last = 0.0, sig.phase
    full_green = durations["G"][1:]  # the first green is shortened by its start offset
    assert all(abs(d - 20.0) < 0.3 for d in full_green)
    assert all(abs(d - 3.0) < 0.3 for d in durations["Y"])
    assert all(abs(d - 2.0) < 0.3 for d in durations["R"])
    assert sig.switches >= 7


def test_axes_alternate_and_never_two_green():
    sig = JunctionSignal.make("J01", 1)
    seen = []
    for _ in range(int(300 / DT)):
        sig.update()
        if sig.phase == "G" and sig.timer >= CONFIG.fixed_green:
            sig.start_switch()
        assert not (sig.light_for("H") == "G" and sig.light_for("V") == "G")
        if not seen or seen[-1] != (sig.green_axis, sig.phase):
            seen.append((sig.green_axis, sig.phase))
    greens = [a for a, p in seen if p == "G"]
    assert all(a != b for a, b in zip(greens, greens[1:]))


# ---------------- pure Python: the metrics recorder ----------------
def make_rec():
    net = build_network()
    return MetricsRecorder(net, CONFIG, amb_route_edges=["W0>J00", "J00>J01", "J01>J02", "J02>E0"])


def test_recorder_waiting_and_queue_counting():
    rec = make_rec()
    veh = {"c0": (0.0, "J00_to_J01"), "c1": (5.0, "J00_to_J01"),
           "c2": (0.2, "W0_to_J00"), "c3": (0.0, "J02_to_E0")}   # c3 is on an exit road: not a queue
    for k in range(8):
        rec.on_step(DT * (k + 1), ["c0", "c1", "c2", "c3"] if k == 0 else [], [], veh)
    assert rec.total_queued == 2 and rec.max_queue == 1
    assert rec.wait["c0"] == pytest.approx(8 * DT) and "c1" not in rec.wait
    assert rec.max_streak == pytest.approx(8 * DT)
    rec.on_step(DT * 9, [], ["c0"], {k: v for k, v in veh.items() if k != "c0"})
    m = rec.metrics()
    assert m["completed"] == 1 and m["entered"] == 4


def test_recorder_ambulance_stops_and_last_junction():
    rec = make_rec()
    t = 0.0

    def step(speed, road, dep=()):
        nonlocal t
        t += DT
        rec.on_step(t, list(dep), [], {"amb0": (speed, road)})

    step(10, "W0_to_J00", dep=["amb0"])
    assert rec.amb_enter == pytest.approx(0.0)
    step(0.1, "J00_to_J01"); step(1.0, "J00_to_J01"); step(0.1, "J00_to_J01")
    assert rec.amb_stops == 1                       # speed never exceeded 1.5, so still the same stop
    step(2.0, "J00_to_J01"); step(0.1, "J01_to_J02")
    assert rec.amb_stops == 2
    assert rec.t_last is None
    step(8.0, ":J02_0_0")                           # left the last junction-bound edge
    assert rec.t_last == pytest.approx(t)


def test_recorder_recovery_rule():
    rec = make_rec()
    rec.baseline, rec.t_last = 5.0, 100.0
    veh = {f"c{i}": (0.0, "J00_to_J01") for i in range(7)}   # 7 queued = baseline + 2: within tolerance
    t = 100.0
    for _ in range(60):
        t += DT
        rec.on_step(t, [], [], veh)
    assert rec.recovery_time == pytest.approx(0.25)          # window opened one step after the ambulance cleared
    rec2 = make_rec()
    rec2.baseline, rec2.t_last = 5.0, 100.0
    veh8 = {f"c{i}": (0.0, "J00_to_J01") for i in range(8)}  # 8 queued: above tolerance
    t = 100.0
    for _ in range(80):
        t += DT
        rec2.on_step(t, [], [], veh8)
    assert rec2.recovery_time is None


# ---------------- SUMO: link mapping and persistence ----------------
@pytest.fixture(scope="module")
def sumo_bank():
    net, path = build_static_network()
    files = build_scenario_files(net, Scenario(seed=7))
    conn = start_sumo(path, files["routes"], 7, "t2_bank")
    bank = SignalBank(conn, net, CONFIG)
    yield conn, bank
    conn.close()


def test_link_states_one_green_per_axis(sumo_bank):
    conn, bank = sumo_bank
    for jid, st in bank.states.items():
        gh, gv = st["green"]["H"], st["green"]["V"]
        n = st["n"]
        assert len(gh) == len(gv) == n == 12
        idx_h = {i for i, c in enumerate(gh) if c in "Gg"}
        idx_v = {i for i, c in enumerate(gv) if c in "Gg"}
        assert idx_h and idx_v and not (idx_h & idx_v)
        assert {st["link_axes"][i] for i in idx_h} == {"H"} and {st["link_axes"][i] for i in idx_v} == {"V"}
        assert all((c == "y") == (g in "Gg") for g, c in zip(gh, st["yellow"]["H"]))


def test_state_is_not_overridden_by_sumo_program(sumo_bank):
    conn, bank = sumo_bank
    bank.apply(force=True)
    for _ in range(int(45 / DT)):                    # longer than SUMO's own 20 s green
        conn.simulationStep()
        bank.audit()
    assert bank.violations == 0 and bank.by_rule["mismatch"] == 0


def test_audit_detects_conflict_and_mismatch(sumo_bank):
    conn, bank = sumo_bank
    before = bank.violations
    jid = bank.net.junction_ids[0]
    both = "".join("G" if "G" in (a + b) else "r" for a, b in zip(bank.states[jid]["green"]["H"], bank.states[jid]["green"]["V"]))
    bank.audit_state(jid, both)
    assert bank.by_rule["conflict"] >= 1 and bank.violations >= before + 2   # conflict and mismatch both counted


# ---------------- whole runs ----------------
def test_strategy_not_ready_yet():
    for sid in ("B", "C"):
        with pytest.raises(NotImplementedError):
            make_strategy(sid)


@pytest.fixture(scope="module")
def run7():
    return run_scenario(Scenario(seed=7, density="medium"), "A", label="t2_run7a")


def test_run_has_all_metrics_and_is_clean(run7):
    m = run7.metrics
    for k in ("ambTravelTime", "ambFinished", "ambStops", "avgWait", "longestWait", "maxQueue", "throughput",
              "completed", "entered", "recoveryTime", "recoveryBaseline", "violations"):
        assert k in m
    assert m["ambFinished"] and 60 < m["ambTravelTime"] < 400
    assert m["avgWait"] > 0 and m["maxQueue"] >= 1 and m["throughput"] > 0 and m["recoveryBaseline"] is not None
    assert m["violations"] == 0 and run7.safety["byRule"] == {"conflict": 0, "mismatch": 0, "min_green": 0}
    kinds = [e["kind"] for e in run7.log]
    assert kinds == ["enter", "pass", "arrive"]
    assert run7.series and run7.switches > 50


def test_run_is_repeatable(run7):
    again = run_scenario(Scenario(seed=7, density="medium"), "A", label="t2_run7b")
    assert again.metrics == run7.metrics and again.log == run7.log
    other_seed = run_scenario(Scenario(seed=8, density="medium"), "A", label="t2_run8")
    assert other_seed.metrics != run7.metrics


def test_blocked_road_run():
    r = run_scenario(Scenario(seed=7, density="low", blocked=("J00|J01",)), "A", label="t2_blk")
    assert r.metrics["ambFinished"] and r.metrics["violations"] == 0


def test_same_order_of_magnitude_as_browser_engine():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(here, "..", "..", "frontend", "baseline.json"))
    if not os.path.exists(path):
        pytest.skip("frontend/baseline.json not found")
    js = json.load(open(path, encoding="utf-8"))["A|low|1"]["metrics"]
    r = run_scenario(Scenario(seed=1, density="low"), "A", label="t2_cmp").metrics
    assert 0.3 < r["ambTravelTime"] / js["ambTravelTime"] < 3.0     # different simulators: same order, not equal

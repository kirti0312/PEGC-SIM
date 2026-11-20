"""Step 10a tests: DemandStream parity, several ambulances (conflict rule, per-ambulance metrics, zero safety
violations), live commands (traffic change, dispatch), and a no-new-features regression.

Pure-Python parts and the fake-TraCI runner tests run anywhere. The API tests need fastapi; the SUMO test skips
without SUMO. The fake TraCI is NOT a traffic simulator (see test_step3_runner.py): it checks wiring and safety
bookkeeping, not traffic realism.
"""
import types

import pytest

import pegc_sumo.runner as runner
from pegc_sumo.ambinfo import Upcoming, free_flow_time
from pegc_sumo.config import CONFIG, AmbulanceSpec, Scenario
from pegc_sumo.demand import DemandStream, generate_ambulances, generate_demand, make_dispatch_spec
from pegc_sumo.live import LiveControl, LiveError
from pegc_sumo.metrics import MetricsRecorder
from pegc_sumo.network import build_network, sumo_edge_id
from pegc_sumo.strategies import CONFLICT_FACTOR, pick_owner

from .test_step3_runner import FakeConn, fake_sumo  # noqa: F401  (fixture reused)

NET = build_network()
STEPS = int(round(CONFIG.max_sim_time / CONFIG.dt))


# ------------------------------------------------------------------ 1. DemandStream parity
def stream_all(s):
    st = DemandStream(NET, s)
    out = []
    for _ in range(STEPS):
        out += st.step()
    return out


@pytest.mark.parametrize("s", [
    Scenario(seed=1, density="low"),
    Scenario(seed=7, density="medium"),
    Scenario(seed=3, density="high"),
    Scenario(seed=9, density="custom", rate=700, blocked=("J00|J01",)),
    Scenario(seed=21, density="custom", rate=100, origin="N0", dest="S1"),
])
def test_stream_yields_exactly_generate_demand(s):
    cars, _ = generate_demand(NET, s)
    assert stream_all(s) == cars and len(cars) > 0


def test_stream_changing_rate_keeps_the_past_and_changes_the_future():
    s = Scenario(seed=5, density="low")
    base, _ = generate_demand(NET, s)
    st = DemandStream(NET, s)
    got, k_change = [], 800
    for k in range(STEPS):
        if k == k_change:
            assert st.set_rate(900) == 900
        got += st.step()
    t_change = k_change * CONFIG.dt
    assert [v for v in got if v.depart < t_change] == [v for v in base if v.depart < t_change]   # past is untouched
    assert len([v for v in got if v.depart >= t_change]) > 2 * len([v for v in base if v.depart >= t_change])
    assert [v.id for v in got] == [f"c{i:05d}" for i in range(len(got))]                          # ids stay consecutive


def test_stream_rate_is_clamped_like_entry_rate():
    st = DemandStream(NET, Scenario(seed=1))
    assert st.set_rate(5) == CONFIG.rate_min and st.set_rate(5000) == CONFIG.rate_max
    assert st.set_rate(float("nan")) == CONFIG.density["medium"]


def test_stream_same_rate_set_again_changes_nothing():
    s = Scenario(seed=4, density="medium")
    st = DemandStream(NET, s)
    got = []
    for k in range(STEPS):
        if k % 97 == 0:
            st.set_rate(st.rate)
        got += st.step()
    assert got == generate_demand(NET, s)[0]


# ------------------------------------------------------------------ 2. conflict rule and ambulance specs
def up(junction, axis, eta, n=0):
    return Upcoming(junction, axis, "e", 0, eta * 12, eta, -1.0, n)


CTX = types.SimpleNamespace(cfg=CONFIG, queue_ahead=None)


def test_same_axis_means_no_conflict_and_earlier_wins():
    owner, conflict = pick_owner(CTX, [("amb1", up("J00", "H", 12)), ("amb0", up("J00", "H", 9))])
    assert owner[0] == "amb0" and conflict is None


def test_earlier_arrival_is_served_first_by_default():
    owner, conflict = pick_owner(CTX, [("amb0", up("J00", "H", 10)), ("amb1", up("J00", "V", 11))])
    assert owner[0] == "amb0" and conflict[0][0] == "amb1"
    owner, _ = pick_owner(CTX, [("amb0", up("J00", "H", 11)), ("amb1", up("J00", "V", 10))])
    assert owner[0] == "amb1"                                           # ties and order of the list do not matter


def test_later_ambulance_wins_only_if_it_would_lose_far_more():
    # the earlier ambulance has a long queue ahead: serving it first would hold the green about 24 s longer, so the
    # later ambulance would lose far more by waiting than the earlier one loses by waiting a short time
    early, late = up("J00", "H", 10, n=12), up("J00", "V", 12, n=0)
    owner, conflict = pick_owner(CTX, [("amb0", early), ("amb1", late)])
    other, other_loss, owner_loss = conflict
    assert owner[0] == "amb1" and other[0] == "amb0" and owner_loss > CONFLICT_FACTOR * other_loss
    # a short queue does not flip the order
    owner, _ = pick_owner(CTX, [("amb0", up("J00", "H", 10, n=1)), ("amb1", up("J00", "V", 12, n=0))])
    assert owner[0] == "amb0"
    # a long queue behind the LATER ambulance does not flip it either: the earlier one would wait for that queue
    owner, _ = pick_owner(CTX, [("amb0", up("J00", "H", 10, n=0)), ("amb1", up("J00", "V", 12, n=12))])
    assert owner[0] == "amb0"


def test_generate_ambulances_ids_and_start_times():
    s = Scenario(seed=1, ambulances=(AmbulanceSpec("amb0", "W0", "E1"), AmbulanceSpec("amb1", "N0", "S0", 90.0)))
    a = generate_ambulances(NET, s)
    assert [x.id for x in a] == ["amb0", "amb1"] and [x.depart for x in a] == [CONFIG.warmup, 90.0]
    d = make_dispatch_spec(NET, 2, "S1", "N1", 123.4)
    assert d.id == "amb2" and d.kind == "amb" and d.depart == 123.4 and d.edges[0].startswith("S1>")


def test_free_flow_time_is_monotone_and_positive():
    assert free_flow_time(0) == 0 and 0 < free_flow_time(50) < free_flow_time(500) < free_flow_time(900)


# ------------------------------------------------------------------ 3. metrics with several ambulances
def test_metrics_single_ambulance_has_no_new_keys():
    rec = MetricsRecorder(NET, CONFIG, amb_route_edges=[e.id for e in NET.edges][:1])
    assert "ambulances" not in rec.metrics() and "ambMeanDelay" not in rec.metrics()


def test_metrics_per_ambulance_and_aggregates():
    rec = MetricsRecorder(NET, CONFIG, amb_route_edges=["W0>J00"])
    rec.register_amb("amb0", ["W0>J00"], 20.0, "W0", "E1")
    rec.register_amb("amb1", ["N0>J00"], 25.0, "N0", "S0")
    rec.on_step(60.25, ["amb0"], [], {"amb0": (10.0, "W0_to_J00")})
    rec.on_step(70.0, ["amb1"], [], {"amb0": (10.0, "W0_to_J00"), "amb1": (10.0, "N0_to_J00")})
    rec.on_step(100.0, [], ["amb0"], {"amb1": (0.0, "N0_to_J00")})
    rec.on_step(130.0, [], ["amb1"], {})
    m = rec.metrics()
    a0, a1 = m["ambulances"]
    assert a0["id"] == "amb0" and a0["finished"] and a0["travelTime"] == pytest.approx(100.0 - 60.0)
    assert a0["delay"] == pytest.approx(a0["travelTime"] - 20.0)
    assert a1["travelTime"] == pytest.approx(130.0 - 69.75) and a1["stops"] == 1
    assert m["ambCount"] == 2 and m["ambAllFinished"]
    assert m["ambMeanDelay"] == pytest.approx((a0["delay"] + a1["delay"]) / 2)
    assert m["ambWorstDelay"] == pytest.approx(max(a0["delay"], a1["delay"]))
    tags = [e["data"].get("amb") for e in rec.log if e["kind"] in ("enter", "arrive")]
    assert "amb1" in tags and m["ambTravelTime"] == pytest.approx(40.0)    # old single-ambulance fields still describe amb0


def test_metrics_unfinished_ambulance_is_reported_not_hidden():
    rec = MetricsRecorder(NET, CONFIG, amb_route_edges=["W0>J00"])
    rec.register_amb("amb0", ["W0>J00"], 20.0)
    rec.register_amb("amb1", ["N0>J00"], 25.0)
    rec.on_step(60.25, ["amb0", "amb1"], [], {"amb0": (10.0, "W0_to_J00"), "amb1": (10.0, "N0_to_J00")})
    rec.on_step(90.0, [], ["amb0"], {"amb1": (5.0, "N0_to_J00")})
    m = rec.metrics()
    assert not m["ambAllFinished"] and m["ambFinishedCount"] == 1 and m["ambulances"][1]["delay"] is None


# ------------------------------------------------------------------ 4. LiveControl
def test_live_control_cap_ids_modified_and_close():
    lv = LiveControl(n_ambulances=1)
    assert not lv.modified
    assert lv.dispatch("N0", "S0") == "amb1" and lv.dispatch("S1", "N1") == "amb2" and lv.modified
    with pytest.raises(LiveError):
        lv.dispatch("W0", "E1")                                          # cap of 3
    lv.set_traffic(400)
    assert lv.drain() == [("dispatch", "amb1", "N0", "S0"), ("dispatch", "amb2", "S1", "N1"), ("traffic", 400.0)]
    assert lv.drain() == []
    lv.close()
    with pytest.raises(LiveError):
        lv.set_traffic(300)


def test_live_control_without_commands_is_not_modified():
    lv = LiveControl(1, speed=None)
    lv.pace(10.0)
    lv.close()
    assert not lv.modified and lv.events == []


# ------------------------------------------------------------------ 5. runner with a fake TraCI that supports several ambulances
class MultiFake(FakeConn):
    """FakeConn with any number of ambulances (from the routes file or added through TraCI) and route/vehicle.add."""

    def __init__(self, net, spec_list):
        super().__init__(net, spec_list[0].edges if spec_list else [])
        self.pending = {s.id: (s.depart, [sumo_edge_id(e) for e in s.edges]) for s in spec_list}
        self.ambs = {}
        self.routes_added, self.cars_added, self.amb_added = {}, [], []
        self.cars = {k: v for k, v in self.cars.items()}
        self.route_api = types.SimpleNamespace(add=lambda rid, edges: self.routes_added.__setitem__(rid, list(edges)))
        self.route = self.route_api                       # conn.route.add(...)
        self.vehicle.add = self._add
        self.vehicle.setSpeedFactor = lambda vid, f: None
        self.vehicle.getRoute = lambda vid: list(self.ambs[vid]["route"]) if vid in self.ambs else []
        self.vehicle.getLength = lambda vid: 4.5

    def _add(self, vid, rid, typeID="car", depart="now", departLane="best", departSpeed="max"):
        if typeID == "amb":
            self.pending[vid] = (self.t, self.routes_added[rid])
            self.amb_added.append((round(self.t, 3), vid))
        else:
            self.cars_added.append((round(self.t, 3), vid, self.routes_added[rid]))

    def _state(self, vid):
        a = self.ambs.get(vid)
        if a is not None:
            return a["road"], a["pos"], a["speed"]
        if vid in self.cars:
            edge, pos = self.cars[vid]
            return edge, pos, 0.0
        return None

    def _lane_vehicles(self, lane):
        edge = lane.rsplit("_", 1)[0]
        return [v for v, (e, _) in self.cars.items() if e == edge] + [v for v, a in self.ambs.items() if a["road"] == edge]

    def simulationStep(self):
        self._departed, self._arrived = [], []
        dt, AMB_SPEED, INTERNAL = CONFIG.dt, 12.0, 10.0
        if self.step_no == 0:
            self._departed += list(self.cars)
        for vid, (dep, route) in list(self.pending.items()):
            if dep <= self.t + 1e-9:
                del self.pending[vid]
                self.ambs[vid] = {"road": route[0], "pos": 0.0, "speed": AMB_SPEED, "k": 0, "route": route}
                self._departed.append(vid)
        for vid, a in list(self.ambs.items()):
            if vid in self._departed:
                continue
            route = a["route"]
            if a["road"].startswith(":"):
                a["pos"] += AMB_SPEED * dt
                if a["pos"] >= INTERNAL:
                    a["k"] += 1
                    a.update(road=route[a["k"]], pos=0.0, speed=AMB_SPEED)
                continue
            edge, length = a["road"], self.len[a["road"]]
            junction = self.net.edge_by_id[self._browser(edge)].to
            at_j = self.net.nodes[junction].type == "junction"
            nxt = a["pos"] + AMB_SPEED * dt
            if at_j and nxt >= length - 2.0 and not self.trafficlight.is_green_for(junction, edge):
                a["pos"], a["speed"] = max(a["pos"], length - 2.0), 0.0
            else:
                a["pos"], a["speed"] = nxt, AMB_SPEED
                if a["pos"] >= length:
                    if not at_j or a["k"] == len(route) - 1:
                        self._arrived.append(vid)
                        del self.ambs[vid]
                    else:
                        a.update(road=f":{junction}_0_0", pos=0.0)
        self.t = round(self.t + dt, 6)
        self.step_no += 1


@pytest.fixture
def multi_sumo(monkeypatch):
    constants = types.ModuleType("traci.constants")
    constants.VAR_SPEED, constants.VAR_ROAD_ID, constants.VAR_LANEPOSITION = 0x40, 0x50, 0x56
    traci = types.ModuleType("traci")
    traci.constants = constants
    import sys
    monkeypatch.setitem(sys.modules, "traci", traci)
    monkeypatch.setitem(sys.modules, "traci.constants", constants)
    created, files = [], {}
    real_build = runner.build_scenario_files

    def build(net_, s, cfg=CONFIG, out_dir=None, stream=False):
        files.clear()
        files.update(real_build(net_, s, cfg, out_dir, stream=stream))
        return files

    def fake_start(net_path, routes_path, seed, label, cfg=CONFIG):
        conn = MultiFake(NET, files["ambs"])
        created.append(conn)
        return conn

    monkeypatch.setattr(runner, "build_static_network", lambda cfg=CONFIG, out_dir=None: (NET, "unused.net.xml"))
    monkeypatch.setattr(runner, "build_scenario_files", build)
    monkeypatch.setattr(runner, "start_sumo", fake_start)
    return created


def amb_scenario(seed, specs, density="low"):
    return Scenario(seed=seed, density=density, origin=specs[0][0], dest=specs[0][1],
                    ambulances=tuple(AmbulanceSpec(f"amb{i}", o, d, t) for i, (o, d, t) in enumerate(specs)))


TWO = [("W0", "E1", None), ("N0", "S0", None)]                    # both reach J00 together on different axes
THREE = [("W0", "E1", None), ("N0", "S0", 62.0), ("S1", "N1", 64.0)]


@pytest.mark.parametrize("strategy", ["A", "B", "C", "C1", "C2"])
@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("specs", [TWO, THREE])
def test_several_ambulances_zero_safety_violations(multi_sumo, strategy, seed, specs):
    r = runner.run_scenario(amb_scenario(seed, specs), strategy, label=f"m_{strategy}_{seed}_{len(specs)}")
    m = r.metrics
    assert m["violations"] == 0 and all(n == 0 for n in r.safety["byRule"].values())
    assert m["ambCount"] == len(specs) and m["ambAllFinished"], m["ambulances"]
    assert [a["id"] for a in m["ambulances"]] == [f"amb{i}" for i in range(len(specs))]
    assert m["ambWorstDelay"] >= m["ambMeanDelay"] > -5
    assert all(a["travelTime"] > 0 and a["freeFlowTime"] > 0 for a in m["ambulances"])
    kinds = [(e["kind"], e["data"].get("amb")) for e in r.log]
    for i in range(1, len(specs)):
        assert ("enter", f"amb{i}") in kinds and ("arrive", f"amb{i}") in kinds
    if strategy in ("B", "C", "C1", "C2"):
        tagged = [e for e in r.log if e["data"].get("amb") and e["kind"] in ("request", "switch", "hold", "prepare", "decline")]
        assert tagged and all(e["text"].startswith(f"[{e['data']['amb']}]") for e in tagged)


def test_conflict_is_logged_and_both_ambulances_are_served_under_b_and_c(multi_sumo):
    for strategy in ("B", "C"):
        r = runner.run_scenario(amb_scenario(1, TWO), strategy, label=f"conf_{strategy}")
        conflicts = [e for e in r.log if e["kind"] == "conflict"]
        assert conflicts and "need different axes" in conflicts[0]["text"], strategy
        assert r.metrics["ambAllFinished"] and r.metrics["violations"] == 0


def test_second_ambulance_waits_at_most_a_bounded_time_under_b_and_c(multi_sumo):
    a = runner.run_scenario(amb_scenario(2, TWO), "A", label="bound_a").metrics
    for strategy in ("B", "C"):
        m = runner.run_scenario(amb_scenario(2, TWO), strategy, label=f"bound_{strategy}").metrics
        # the lower-priority ambulance is delayed but never starved (safety layer hold cap and max wait still apply)
        assert m["ambWorstTravelTime"] < a["ambWorstTravelTime"] + 60.0


# ---- live runs
def test_live_run_without_commands_inserts_exactly_the_batch_cars(multi_sumo):
    s = Scenario(seed=7, density="low")
    lv = LiveControl(1)
    r = runner.run_scenario(s, "A", label="live0", live=lv)
    conn = multi_sumo[0]
    cars, _ = generate_demand(NET, s)
    assert [(t, vid) for t, vid, _ in conn.cars_added] == [(v.depart, v.id) for v in cars]
    assert [route for _, _, route in conn.cars_added] == [[sumo_edge_id(e) for e in v.edges] for v in cars]
    assert not lv.modified and lv.events == [] and lv.finished
    assert "ambulances" not in r.metrics                                  # one ambulance, no dispatch: same metric keys as before


def test_live_traffic_change_and_dispatch_apply_at_the_right_time(multi_sumo):
    s = Scenario(seed=7, density="low")
    lv = LiveControl(1)
    fired = {}

    def on_step(snap):
        if snap["t"] >= 100.0 and "traffic" not in fired:
            fired["traffic"] = snap["t"]
            lv.set_traffic(800)
        if snap["t"] >= 130.0 and "dispatch" not in fired:
            fired["dispatch"] = snap["t"]
            assert lv.dispatch("S1", "N1") == "amb1"

    r = runner.run_scenario(s, "C", label="live1", live=lv, on_step=on_step)
    conn = multi_sumo[0]
    assert lv.modified and [e["kind"] for e in lv.events] == ["traffic", "dispatch"]
    t_traffic = lv.events[0]["t"]
    assert t_traffic == pytest.approx(fired["traffic"], abs=CONFIG.dt * 1.5) and lv.events[0]["rate"] == 800
    before = [c for c in conn.cars_added if c[0] < t_traffic]
    base, _ = generate_demand(NET, s)
    assert before == [(v.depart, v.id, [sumo_edge_id(e) for e in v.edges]) for v in base if v.depart < t_traffic]
    after_rate = len([c for c in conn.cars_added if c[0] >= t_traffic]) / (CONFIG.max_sim_time - t_traffic)
    assert after_rate > 2 * len(before) / t_traffic                       # clearly denser after the change
    assert conn.amb_added and conn.amb_added[0][1] == "amb1" and conn.amb_added[0][0] == pytest.approx(lv.events[1]["t"])
    m = r.metrics
    assert m["ambCount"] == 2 and m["ambAllFinished"] and m["violations"] == 0
    assert any(e["kind"] == "live" and "traffic set to 800" in e["text"] for e in r.log)
    assert any(e["kind"] == "live" and e["data"].get("amb") == "amb1" for e in r.log)


def test_live_dispatch_three_ambulances_zero_violations_and_cap(multi_sumo):
    for strategy in ("B", "C"):
        lv = LiveControl(1)
        sent = []

        def on_step(snap, lv=lv, sent=sent):
            for t_at, o, d in ((90.0, "N0", "S0"), (91.0, "S1", "N1")):
                if snap["t"] >= t_at and t_at not in sent:
                    sent.append(t_at)
                    lv.dispatch(o, d)
            if snap["t"] >= 95.0 and "cap" not in sent:
                sent.append("cap")
                with pytest.raises(LiveError):
                    lv.dispatch("W1", "E0")

        r = runner.run_scenario(Scenario(seed=3, density="low"), strategy, label=f"live3_{strategy}", live=lv, on_step=on_step)
        assert r.metrics["ambCount"] == 3 and r.metrics["violations"] == 0 and "cap" in sent
        assert all(n == 0 for n in r.safety["byRule"].values())


def test_snapshots_carry_all_ambulances_only_when_there_are_several(multi_sumo):
    snaps = []
    runner.run_scenario(amb_scenario(1, TWO), "B", label="snap2", on_step=snaps.append)
    both = [x for x in snaps if "ambs" in x and len(x["ambs"]) == 2]
    assert both and all({a["id"] for a in x["ambs"]} == {"amb0", "amb1"} for x in both)
    assert all("amb" in x for x in snaps)                                  # the old single `amb` key is still there


# ------------------------------------------------------------------ 6. no-new-features regression
def test_single_ambulance_run_is_unchanged_in_shape_and_has_no_new_keys(multi_sumo):
    s = Scenario(seed=7, density="low")
    r = runner.run_scenario(s, "C", label="reg1")
    assert set(r.metrics) == {"waitP95", "ambTravelTime", "ambFinished", "ambStops", "avgWait", "longestWait", "maxQueue",
                              "throughput", "completed", "entered", "recoveryTime", "recoveryBaseline", "totalQueued",
                              "time", "violations"}
    assert not any("amb" in (e["data"].get("amb") or "") for e in r.log)    # no log tags in a one-ambulance run
    conn = multi_sumo[0]
    assert conn.cars_added == [] and conn.amb_added == []                    # cars come from the routes file, nothing inserted
    # the run context of a one-ambulance run keeps the old single-tracker path
    ctx = runner.RunContext(CONFIG, None)
    assert ctx.ambs is None and ctx.log_tag is None


def test_explicit_single_ambulance_list_equals_the_default_scenario(multi_sumo):
    a = runner.run_scenario(Scenario(seed=5, density="low"), "C", label="reg_a")
    b = runner.run_scenario(amb_scenario(5, [("W0", "E1", None)]), "C", label="reg_b")
    assert a.metrics == b.metrics and a.log == b.log and a.switches == b.switches


# ------------------------------------------------------------------ 7. API (needs fastapi) and SUMO
@pytest.fixture
def api_client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import json
    from fastapi.testclient import TestClient
    from api.main import create_app

    import threading
    gate = threading.Event()
    gate.set()                                                           # a test clears it to keep a live run open

    def runner_stub(s, strategy, label, cfg, on_step, live=None):
        calls = {"live": live}
        if live is not None:
            gate.wait(10)
        from pegc_sumo.runner import RunResult
        m = {"ambTravelTime": 50.0, "avgWait": 5.0, "longestWait": 9.0}
        if len(s.ambulances) > 1:
            m["ambulances"] = [{"id": a.id} for a in s.ambulances]
        if live is not None:
            live.close()
        return RunResult(strategy, s, m, [], [], {"violations": 0}, 0, 0.0, "x", extra=calls)

    import os
    cal = os.path.join(os.path.dirname(__file__), "..", "experiments", "calibration.json")
    client = TestClient(create_app(runner=runner_stub, calibration_path=cal, snapshot_interval=0.001))
    client.gate = gate
    return client


def wait_done(c, rid):
    import time
    for _ in range(300):
        d = c.get(f"/api/runs/{rid}").json()
        if d["status"] in ("done", "error"):
            return d
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_api_old_request_unchanged_and_new_fields_only_for_live(api_client):
    r = api_client.post("/api/runs", json={"strategy": "A", "density": "low"})
    assert r.status_code == 202 and "live_enabled" not in r.json() and "ambulances" not in r.json()
    d = wait_done(api_client, r.json()["id"])
    assert "modified_live" not in d and "live_events" not in d
    assert api_client.post(f"/api/runs/{r.json()['id']}/traffic", json={"rate": 300}).status_code == 409


def test_api_ambulances_validation(api_client):
    ok = api_client.post("/api/runs", json={"strategy": "B", "ambulances": [{"origin": "W0", "dest": "E1"}, {"origin": "N0", "dest": "S0", "start": 70}]})
    assert ok.status_code == 202 and [a["id"] for a in ok.json()["ambulances"]] == ["amb0", "amb1"]
    too_many = {"strategy": "B", "ambulances": [{"origin": "W0", "dest": "E1"}] * 4}
    assert api_client.post("/api/runs", json=too_many).status_code == 422
    same = {"ambulances": [{"origin": "W0", "dest": "W0"}]}
    assert api_client.post("/api/runs", json=same).status_code == 422
    assert api_client.post("/api/runs", json={"ambulances": [{"origin": "W0", "dest": "E1", "start": 999}]}).status_code == 422


def test_api_live_commands_mark_run_modified(api_client):
    api_client.gate.clear()                                              # keep the stub run open while commands arrive
    r = api_client.post("/api/runs", json={"strategy": "C", "density": "low", "live": True})
    assert r.status_code == 202 and r.json()["live_enabled"] is True and r.json()["modified_live"] is False
    rid = r.json()["id"]
    t = api_client.post(f"/api/runs/{rid}/traffic", json={"rate": 400})
    assert t.status_code == 200 and t.json()["modified_live"] is True and t.json()["clean_comparison"] is False
    assert api_client.post(f"/api/runs/{rid}/traffic", json={"rate": 400, "density": "low"}).status_code == 422
    assert api_client.post(f"/api/runs/{rid}/traffic", json={}).status_code == 422
    d = api_client.post(f"/api/runs/{rid}/dispatch", json={"origin": "N0", "dest": "S0"})
    assert d.status_code == 200 and d.json()["ambulance_id"] == "amb1"
    assert api_client.post(f"/api/runs/{rid}/dispatch", json={"origin": "S1", "dest": "N1"}).json()["ambulance_id"] == "amb2"
    assert api_client.post(f"/api/runs/{rid}/dispatch", json={"origin": "W1", "dest": "E0"}).status_code == 409     # cap 3
    assert api_client.post(f"/api/runs/{rid}/dispatch", json={"origin": "W0", "dest": "W0"}).status_code == 422
    api_client.gate.set()
    fin = wait_done(api_client, rid)
    assert fin["modified_live"] is True and fin["summary"]["clean_comparison"] is False
    assert api_client.post(f"/api/runs/{rid}/traffic", json={"rate": 300}).status_code == 409                      # finished


def test_api_live_run_without_commands_stays_clean(api_client):
    rid = api_client.post("/api/runs", json={"strategy": "A", "live": True}).json()["id"]
    d = wait_done(api_client, rid)
    assert d["modified_live"] is False and d["clean_comparison"] is True and d["live_events"] == []


def test_api_unknown_run_for_live_commands(api_client):
    assert api_client.post("/api/runs/nope/traffic", json={"rate": 300}).status_code == 404
    assert api_client.post("/api/runs/nope/dispatch", json={"origin": "W0", "dest": "E1"}).status_code == 404


def sumo_available():
    try:
        from pegc_sumo.sumo_io import find_binary
        find_binary("sumo")
        import traci  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not sumo_available(), reason="needs SUMO")
def test_sumo_live_stream_without_commands_matches_the_clean_run():
    """Real SUMO: cars inserted through TraCI must reproduce the routes-file run (checks the insertion timing)."""
    s = Scenario(seed=7, density="low")
    clean = runner.run_scenario(s, "A", label="t10_clean")
    live = runner.run_scenario(s, "A", label="t10_live", live=LiveControl(1))
    assert live.metrics == clean.metrics and live.switches == clean.switches


@pytest.mark.skipif(not sumo_available(), reason="needs SUMO")
@pytest.mark.parametrize("strategy", ["B", "C"])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_sumo_two_and_three_ambulances_zero_violations(strategy, seed):
    for specs in (TWO, THREE):
        r = runner.run_scenario(amb_scenario(seed, specs, "medium"), strategy, label=f"t10_{strategy}_{seed}_{len(specs)}")
        assert r.metrics["violations"] == 0 and r.metrics["ambCount"] == len(specs)

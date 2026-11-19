"""Step 9a tests: SUMO positions -> browser renderer format, snapshot sampling with frames and log, WebSocket payload.
Pure-Python parts run anywhere; the API test needs fastapi; the SUMO test skips without SUMO."""
import json
import time
from types import SimpleNamespace

import pytest

from api import service as sv
from pegc_sumo.config import CONFIG
from pegc_sumo.frame import BRAKE_DECEL, FrameBuilder, browser_edge_id, car_color, internal_junction
from pegc_sumo.network import build_network

NET = build_network()
SUMO_LEN = {"W0_to_J00": 172.8, "J00_to_J01": 205.6}


def fb(routes=None):
    return FrameBuilder(NET, CONFIG, lambda e: SUMO_LEN.get(e, 205.6), lambda vid: (routes or {}).get(vid, []))


def test_edge_id_mapping():
    assert browser_edge_id("W0_to_J00") == "W0>J00"
    assert browser_edge_id("J00_to_J01") == "J00>J01"
    assert browser_edge_id(":J00_3") is None and browser_edge_id("") is None and browser_edge_id("junk") is None
    assert internal_junction(":J00_3_0") == "J00" and internal_junction(":J12_10") == "J12" and internal_junction("W0_to_J00") is None


def test_every_network_edge_maps_to_a_browser_edge():
    from pegc_sumo.network import sumo_edge_id
    for e in NET.edges:
        assert browser_edge_id(sumo_edge_id(e.id)) == e.id and e.id in NET.edge_by_id


def test_distance_is_scaled_to_browser_length():
    b = fb()
    e, s, prev = b.pose("c1", "J00_to_J01", 205.6)       # end of the SUMO lane = end of the browser edge
    assert (e, prev) == ("J00>J01", None) and s == pytest.approx(220.0)
    assert b.pose("c1", "J00_to_J01", 102.8)[1] == pytest.approx(110.0)
    assert b.pose("c1", "W0_to_J00", 172.8)[1] == pytest.approx(180.0)     # boundary edge: 172.8 -> 180
    assert b.pose("c1", "W0_to_J00", 0.0)[1] == 0.0
    assert b.pose("c1", "J00_to_J01", 999)[1] == pytest.approx(220.0)      # clamped
    assert b.pose("c1", "J00_to_J01", -1)[1] == 0.0


def test_internal_lane_goes_to_next_edge_at_zero():
    b = fb({"c1": ["W0_to_J00", "J00_to_J01", "J01_to_J02", "J02_to_E0"]})
    assert b.pose("c1", ":J00_3_0", 4.2) == ("J00>J01", 0.0, "W0>J00")
    assert b.pose("c1", ":J00_3", 0.1) == ("J00>J01", 0.0, "W0>J00")
    assert b.pose("c1", ":J02_0_0", 1.0) == ("J02>E0", 0.0, "J01>J02")
    assert b.pose("c1", ":J11_0_0", 1.0) is None            # route does not pass J11 -> cannot place
    assert b.pose("c1", "weird", 1.0) is None


def test_vehicle_fields_and_ambulance():
    b = fb()
    v = b.vehicle("amb0", "J00_to_J01", 50.0, 14.0)
    assert v["kind"] == "amb" and v["len"] == CONFIG.amb_length and v["color"] == 0 and v["edge"] == "J00>J01"
    c = b.vehicle("veh12", "J00_to_J01", 50.0, 3.0, prev_speed=3.0 + BRAKE_DECEL * CONFIG.dt * 1.01)
    assert c["kind"] == "car" and c["len"] == CONFIG.car_length and 0 <= c["color"] < 6 and c["braking"] is True
    assert b.vehicle("veh12", "J00_to_J01", 50.0, 3.0, prev_speed=3.0)["braking"] is False
    assert b.vehicle("veh12", "J00_to_J01", 50.0, 3.0)["braking"] is False
    assert car_color("veh12") == car_color("veh12")
    json.dumps(c)


def test_frame_skips_unplaceable_vehicles():
    b = fb({"a": ["W0_to_J00", "J00_to_J01"]})
    out = b.frame([("a", ":J00_1_0", 1.0, 5.0), ("b", ":J00_1_0", 1.0, 5.0), ("c", "W0_to_J00", 10.0, 5.0)])
    assert [v["id"] for v in out] == ["a", "c"]


# ------------------------------------------------------------------ sampler (service)
def _mgr(interval=0.05):
    return sv.RunManager(lambda *a: None, snapshot_interval=interval)


def _run(m):
    return sv.Run("r1", "run", ("A",), None, {})


def test_sampler_carries_log_of_skipped_steps_and_flags_frames():
    m = _mgr(10.0)                                   # huge interval: only the first step is kept
    run = _run(m)
    cb = m._sampler(run)
    assert cb.frame_wanted() is True
    cb({"t": 1.0, "log": [{"t": 1.0, "kind": "a"}], "veh": [{"id": "x"}]})
    for k in range(2, 5):
        assert cb.frame_wanted() is False
        cb({"t": float(k), "log": [{"t": float(k), "kind": "b"}]})
    assert len(run.snapshots) == 1 and run.snapshots[0]["veh"] == [{"id": "x"}]
    assert [e["kind"] for e in run.snapshots[0]["log"]] == ["a"]
    time.sleep(0.01)
    m.interval = 0.0
    assert cb.frame_wanted() is True
    cb({"t": 5.0, "log": [], "veh": []})
    assert [e["kind"] for e in run.snapshots[1]["log"]] == ["b", "b", "b"]      # nothing lost
    json.dumps(run.snapshots)


def test_sampler_without_frame_support_still_samples_by_clock():
    m = _mgr(0.0)
    run = _run(m)
    cb = m._sampler(run)
    for k in range(3):
        cb({"t": float(k), "queued": 0, "vehicles": 1, "signals": {}, "amb": None})   # old-style snapshot, no log/veh
    assert len(run.snapshots) == 3 and run.snapshots[0]["log"] == []


def test_worker_keeps_full_log_for_summary():
    def runner(s, strategy, label, cfg, on_step):
        return SimpleNamespace(strategy=strategy, metrics={"violations": 0}, safety={}, switches=0, wall_seconds=0.0,
                               routes_sha1="x", log=[{"t": 1.0, "kind": "request", "text": "t", "data": {"v": float("nan")}}])
    m = sv.RunManager(runner)
    s, meta = sv.resolve("W0", "E1", "low", None, 1, [], {"scale": 0.7, "status": "ok"})
    run = m.submit("run", ("C",), s, meta)
    assert run.done.wait(5) and run.log[0]["data"]["v"] is None


# ------------------------------------------------------------------ real runner loop against the fake TraCI of Step 3
from .test_step3_runner import FakeConn, fake_sumo  # noqa: E402,F401  (fixture reused)
import pegc_sumo.runner as runner  # noqa: E402
from pegc_sumo.config import Scenario  # noqa: E402


@pytest.fixture
def fake_with_route(fake_sumo, monkeypatch):
    orig = FakeConn.__init__

    def init(self, net, amb_edges):
        orig(self, net, amb_edges)
        self.vehicle.getRoute = lambda vid: list(self.route)
    monkeypatch.setattr(FakeConn, "__init__", init)
    return fake_sumo


def test_runner_streams_frames_log_and_changes_nothing(fake_with_route):
    s = Scenario(seed=7, density="low")
    plain = runner.run_scenario(s, "C", label="f0")
    snaps = []
    r = runner.run_scenario(s, "C", label="f1", on_step=snaps.append)
    assert plain.metrics == r.metrics and plain.switches == r.switches            # on_step changes no result
    assert len(snaps) == int(round(CONFIG.max_sim_time / CONFIG.dt))
    assert all("veh" in x and "signals" in x for x in snaps)
    assert sum(len(x["log"]) for x in snaps) == len(r.log)                         # each log entry streamed once
    assert all("timer" in sg for sg in snaps[0]["signals"].values())
    placed = [x["amb"] for x in snaps if x["amb"] and "edge" in x["amb"]]
    assert placed and all(a["edge"] in NET.edge_by_id for a in placed)
    inside = [a for a in placed if a["prev"] is not None]                          # crossed a junction: next edge at s = 0
    assert inside and all(a["s"] == 0.0 for a in inside)
    amb_vs = [v for x in snaps for v in x["veh"] if v["id"] == "amb0"]
    assert amb_vs and all(v["kind"] == "amb" for v in amb_vs)
    for x in snaps:
        for v in x["veh"]:
            assert 0.0 <= v["s"] <= NET.edge_by_id[v["edge"]].length + 1e-6
    json.dumps(sv.clean(snaps[-1]))


def test_runner_builds_frames_only_when_wanted(fake_with_route):
    snaps, asked = [], []

    def cb(x):
        snaps.append(x)
    cb.frame_wanted = lambda: asked.append(1) or len(asked) % 4 == 0
    runner.run_scenario(Scenario(seed=7, density="low"), "A", label="f2", on_step=cb)
    framed = [x for x in snaps if "veh" in x]
    assert len(asked) == len(snaps) and 0 < len(framed) == len(snaps) // 4


# ------------------------------------------------------------------ API (needs fastapi)
def test_websocket_stream_has_vehicles_signals_amb_and_log():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from api.main import create_app

    def runner(s, strategy, label, cfg, on_step):
        for k in range(3):
            on_step({"t": float(k), "queued": 1, "vehicles": 1,
                     "signals": {"J00": {"phase": "G", "green_axis": "H", "timer": 2.0}},
                     "amb": {"road": "W0_to_J00", "pos": 10.0, "speed": 5.0, "edge": "W0>J00", "s": 10.4, "prev": None},
                     "log": [{"t": float(k), "kind": "request", "text": "x", "data": {}}],
                     "veh": [{"id": "amb0", "kind": "amb", "edge": "W0>J00", "s": 10.4, "v": 5.0, "color": 0,
                              "len": 6.5, "braking": False, "prev": None}]})
        return SimpleNamespace(strategy=strategy, metrics={"violations": 0}, safety={}, switches=0, wall_seconds=0.0,
                               routes_sha1="x", log=[{"t": 0.0, "kind": "request", "text": "x", "data": {}}])
    c = TestClient(create_app(runner, snapshot_interval=0.0))
    rid = c.post("/api/runs", json={"strategy": "C", "density": "low"}).json()["id"]
    with c.websocket_connect(f"/ws/runs/{rid}") as ws:
        msgs = []
        while True:
            m = ws.receive_json()
            msgs.append(m)
            if m["type"] != "snapshot":
                break
    snaps = [m for m in msgs if m["type"] == "snapshot"]
    assert snaps and snaps[0]["veh"][0]["edge"] == "W0>J00" and snaps[0]["amb"]["edge"] == "W0>J00"
    assert snaps[0]["signals"]["J00"]["timer"] == 2.0 and snaps[0]["log"][0]["kind"] == "request"
    assert msgs[-1]["type"] == "summary" and msgs[-1]["log"][0]["kind"] == "request" and msgs[-1]["engine"] == "SUMO"


# ------------------------------------------------------------------ SUMO (skips without SUMO)
def _sumo():
    try:
        from pegc_sumo.sumo_io import find_binary
        find_binary("sumo")
        import traci  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _sumo(), reason="SUMO not installed")
def test_real_run_frames_are_valid_browser_positions():
    from pegc_sumo.config import Scenario
    from pegc_sumo.runner import run_scenario
    s = Scenario(density="custom", rate=231.0, seed=7)
    snaps = []
    a = run_scenario(s, "A", label="t9a_a")
    b = run_scenario(s, "C", label="t9a_b", on_step=snaps.append)
    assert a.strategy == "A" and b.metrics["violations"] == 0
    frames = [x for x in snaps if "veh" in x]
    assert len(frames) == len(snaps) and len(frames) > 100
    ids = set()
    for x in frames:
        for v in x["veh"]:
            e = NET.edge_by_id[v["edge"]]
            assert 0.0 <= v["s"] <= e.length + 1e-6
            ids.add(v["id"])
    assert "amb0" in ids and any(x["amb"] and x["amb"].get("edge") for x in snaps)
    assert sum(len(x["log"]) for x in snaps) == len(b.log)           # every log entry streamed exactly once
    json.dumps(sv.clean(snaps[-1]))
    r2 = run_scenario(s, "C", label="t9a_c")                         # frames change no metric
    assert r2.metrics == b.metrics

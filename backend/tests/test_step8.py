"""Step 8 tests. The run is stubbed (no SUMO). API tests need fastapi+httpx (pip install -r requirements.txt)."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from api import service as sv
from pegc_sumo.config import CONFIG

CAL = {"scale": 0.7, "status": "ok"}


def fake_runner_factory(log=None, fail_on=None, delay=0.0):
    state = {"active": 0, "max_active": 0, "calls": []}

    def runner(s, strategy, label, cfg, on_step):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        state["calls"].append((strategy, s.rate, s.density, s.seed, label, cfg.c3_wait_weight))
        try:
            if fail_on == strategy:
                raise RuntimeError("boom")
            for k in range(5):
                if on_step:
                    on_step({"t": float(k), "queued": k, "vehicles": 3,
                             "signals": {"J00": {"phase": "G", "green_axis": "H"}}, "amb": None})
                time.sleep(delay)
            m = {"ambTravelTime": {"A": 100.0, "B": 90.0, "C": 80.0}.get(strategy, 70.0), "avgWait": 10.0,
                 "longestWait": 20.0 if strategy != "A" else 0.0, "violations": 0, "x": float("nan")}
            return SimpleNamespace(strategy=strategy, metrics=m, safety={"violations": 0}, switches=4,
                                   wall_seconds=0.1, routes_sha1="abc")
        finally:
            state["active"] -= 1
    return runner, state


# ------------------------------------------------------------------ service (pure Python)
def test_presets_use_calibrated_rates_and_label():
    s, meta = sv.resolve("W0", "E1", "high", None, 7, [], CAL)
    assert (s.density, s.rate) == ("custom", 364.0)            # 520 x 0.7, same as experiments.batch.rate_for
    assert meta["engine"] == "SUMO" and meta["calibration_scale"] == 0.7
    assert meta["oversaturated"] is True and "oversaturated" in meta["traffic_note"]
    assert sv.resolve("W0", "E1", "low", None, 7, [], CAL)[0].rate == 126.0
    assert sv.resolve("W0", "E1", "medium", None, 7, [], CAL)[1]["oversaturated"] is False


def test_custom_rate_rules():
    s, meta = sv.resolve("W0", "E1", "custom", 250, 1, [], CAL)
    assert s.rate == 250.0 and meta["oversaturated"] is None
    for bad in (99, 901, None, "x", True):
        with pytest.raises(sv.InputError):
            sv.resolve("W0", "E1", "custom", bad, 1, [], CAL)


@pytest.mark.parametrize("args", [("X9", "E1"), ("W0", "W0"), ("W0", "J00"), ("J00", "E1")])
def test_bad_nodes(args):
    with pytest.raises(sv.InputError):
        sv.resolve(args[0], args[1], "low", None, 1, [], CAL)


def test_seed_and_blocked_rules():
    for bad in (1.5, "7", True, None):
        with pytest.raises(sv.InputError):
            sv.resolve("W0", "E1", "low", None, bad, [], CAL)
    with pytest.raises(sv.InputError):
        sv.resolve("W0", "E1", "low", None, 1, ["nope"], CAL)
    from experiments.batch import pick_blocked
    net = sv._net()
    seg = pick_blocked(net, "W0", "E1")
    assert sv.resolve("W0", "E1", "low", None, 1, [seg], CAL)[1]["blocked"] == [seg]
    every = sorted({e.seg_id for e in net.edges if net.nodes[e.frm].type == "junction" and net.nodes[e.to].type == "junction"})
    with pytest.raises(sv.InputError, match="no valid route"):
        sv.resolve("W0", "E1", "low", None, 1, every, CAL)


def test_presets_need_calibration():
    with pytest.raises(sv.InputError):
        sv.resolve("W0", "E1", "low", None, 1, [], {"scale": None, "status": "missing"})
    assert sv.load_calibration("/nonexistent.json")["scale"] is None
    assert sv.load_calibration()["scale"] == json.load(open(sv.CALIBRATION_PATH))["scale"]


def test_c3_weight_source():
    assert sv.load_c3()["weight"] == json.load(open(sv.C3_TUNING_PATH))["choice"]["weight"]
    assert sv.load_c3("/nonexistent.json") == {"weight": CONFIG.c3_wait_weight, "source": "config default", "status": None}


def test_clean_and_deltas():
    assert sv.clean({"a": float("inf"), "b": [float("nan"), 1.0]}) == {"a": None, "b": [None, 1.0]}
    r = {"A": {"metrics": {"ambTravelTime": 100.0, "avgWait": 0, "longestWait": 10.0}},
         "B": {"metrics": {"ambTravelTime": 90.0, "avgWait": 5.0, "longestWait": 12.0}}}
    d = sv.deltas_vs_a(r)["B"]
    assert d == {"ambTravelTime": -10.0, "avgWait": None, "longestWait": 20.0}


def test_manager_runs_one_at_a_time_in_order():
    runner, st = fake_runner_factory(delay=0.01)
    m = sv.RunManager(runner, snapshot_interval=0.0)
    sc, meta = sv.resolve("W0", "E1", "medium", None, 3, [], CAL)
    runs = [m.submit("run", ("A",), sc, meta), m.submit("compare", ("A", "B", "C"), sc, meta), m.submit("run", ("C3",), sc, meta)]
    for r in runs:
        assert r.done.wait(10) and r.status == "done"
    assert st["max_active"] == 1
    assert [c[0] for c in st["calls"]] == ["A", "A", "B", "C", "C3"]
    assert len({c[4] for c in st["calls"]}) == 5                    # every TraCI label unique
    assert st["calls"][-1][5] == m.c3["weight"] and st["calls"][0][5] == CONFIG.c3_wait_weight
    assert runs[0].summary["metrics"]["x"] is None and len(runs[0].snapshots) == 5
    assert runs[1].snapshots == [] and set(runs[1].results) == {"A", "B", "C"}
    assert runs[2].meta["c3_weight"] == m.c3["weight"]


def test_manager_error_and_queue_limit():
    runner, _ = fake_runner_factory(fail_on="B")
    m = sv.RunManager(runner, snapshot_interval=0.0)
    sc, meta = sv.resolve("W0", "E1", "low", None, 1, [], CAL)
    r = m.submit("run", ("B",), sc, meta)
    assert r.done.wait(10) and r.status == "error" and "boom" in r.error
    gate = threading.Event()
    m2 = sv.RunManager(lambda *a: gate.wait(5) or fake_runner_factory()[0](*a), snapshot_interval=0.0, max_queued=2)
    m2.submit("run", ("A",), sc, meta); m2.submit("run", ("A",), sc, meta)
    with pytest.raises(sv.Busy):
        m2.submit("run", ("A",), sc, meta)
    gate.set()


def test_snapshot_sampling_rate():
    runner, _ = fake_runner_factory(delay=0.03)
    m = sv.RunManager(runner, snapshot_interval=0.1)
    sc, meta = sv.resolve("W0", "E1", "low", None, 1, [], CAL)
    r = m.submit("run", ("A",), sc, meta)
    r.done.wait(10)
    assert 1 <= len(r.snapshots) < 5                               # sampled by wall time, not every step


# ------------------------------------------------------------------ API (needs fastapi)
@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from api.main import create_app
    runner, st = fake_runner_factory()
    c = TestClient(create_app(runner=runner, snapshot_interval=0.01))
    c.state = st
    return c


def wait_done(c, rid):
    for _ in range(200):
        r = c.get(f"/api/runs/{rid}").json()
        if r["status"] in ("done", "error"):
            return r
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_health(client):
    h = client.get("/api/health").json()
    assert h["engine"] == "SUMO" and h["calibration_scale"] == 0.7 and "W0" in h["boundary_nodes"]
    assert "oversaturated" in h["high_traffic"]


def test_start_and_read_back(client):
    r = client.post("/api/runs", json={"strategy": "C", "density": "high", "seed": 11})
    assert r.status_code == 202
    body = r.json()
    assert body["engine"] == "SUMO" and body["calibration_scale"] == 0.7 and body["oversaturated"] is True
    done = wait_done(client, body["id"])
    assert done["status"] == "done" and done["summary"]["metrics"]["ambTravelTime"] == 80.0
    assert done["engine"] == "SUMO" and done["rate_per_entry_veh_h"] == 364
    assert client.state["calls"][0][1:4] == (364.0, "custom", 11)


@pytest.mark.parametrize("payload", [
    {"strategy": "Z"}, {"origin": "J00"}, {"dest": "W0"}, {"density": "huge"}, {"seed": 1.5}, {"seed": "x"},
    {"density": "custom"}, {"density": "custom", "rate": 50}, {"density": "custom", "rate": 1000},
    {"blocked": ["nope"]}, {"extra": 1}])
def test_validation_422(client, payload):
    assert client.post("/api/runs", json=payload).status_code == 422
    assert client.state["calls"] == []


def test_unknown_run_404(client):
    assert client.get("/api/runs/nope").status_code == 404


def test_compare(client):
    r = client.get("/api/compare", params={"density": "medium", "seed": 5})
    assert r.status_code == 200
    b = r.json()
    assert [c[0] for c in client.state["calls"]] == ["A", "B", "C"] and client.state["max_active"] == 1
    assert b["engine"] == "SUMO" and b["calibration_scale"] == 0.7 and set(b["results"]) == {"A", "B", "C"}
    assert b["percent_change_vs_A"]["C"]["ambTravelTime"] == -20.0
    assert client.get("/api/compare", params={"origin": "bad"}).status_code == 422
    assert client.get("/api/compare", params={"density": "custom"}).status_code == 422
    assert client.get("/api/compare", params={"blocked": "nope"}).status_code == 422


def test_websocket_snapshots_then_summary(client):
    rid = client.post("/api/runs", json={"strategy": "A", "density": "low"}).json()["id"]
    with client.websocket_connect(f"/ws/runs/{rid}") as ws:
        msgs = []
        while True:
            m = ws.receive_json()
            msgs.append(m)
            if m["type"] != "snapshot":
                break
    assert [m["type"] for m in msgs[:-1]] == ["snapshot"] * (len(msgs) - 1) and len(msgs) >= 2
    assert msgs[0]["engine"] == "SUMO" and msgs[0]["signals"]["J00"]["phase"] == "G"
    assert msgs[-1]["type"] == "summary" and msgs[-1]["summary"]["strategy"] == "A" and msgs[-1]["engine"] == "SUMO"


def test_websocket_unknown_and_error(client):
    with client.websocket_connect("/ws/runs/nope") as ws:
        assert ws.receive_json()["type"] == "error"


def test_cors_header(client):
    r = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


# ------------------------------------------------------------------ SUMO (skips without SUMO)
def _sumo():
    try:
        from pegc_sumo.sumo_io import find_binary
        find_binary("sumo")
        import traci  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _sumo(), reason="SUMO not installed")
def test_on_step_callback_does_not_change_results():
    from pegc_sumo.config import Scenario
    from pegc_sumo.runner import run_scenario
    s = Scenario(density="custom", rate=231.0, seed=7)
    snaps = []
    a = run_scenario(s, "A", label="t8_a")
    b = run_scenario(s, "A", label="t8_b", on_step=snaps.append)
    assert a.metrics == b.metrics and a.switches == b.switches and a.routes_sha1 == b.routes_sha1
    assert len(snaps) == int(round(CONFIG.max_sim_time / CONFIG.dt)) and "signals" in snaps[0]
    json.dumps(sv.clean(snaps[-1]))

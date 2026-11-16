"""Step 1 tests: network parity with the browser engine, demand parity, SUMO smoke runs."""
import json
import math
import os

import pytest

from pegc_sumo.config import CONFIG, Scenario, entry_rate
from pegc_sumo.demand import generate_demand
from pegc_sumo.network import build_network, find_route, sumo_edge_id
from pegc_sumo.rng import Rng, derive_seed
from pegc_sumo.smoke import run_smoke
from pegc_sumo.sumo_io import build_scenario_files, build_static_network

GOLD_PATH = os.path.join(os.path.dirname(__file__), "golden_js.json")
GOLD = json.load(open(GOLD_PATH, encoding="utf-8"))


# ---- parity with the browser engine (reference data exported from the real JS code) ----
def test_rng_matches_js():
    r = Rng(42)
    got = [r.next() for _ in range(8)]
    assert got == pytest.approx(GOLD["rngFirst"], abs=1e-15)
    assert [derive_seed(7, 1), derive_seed(7, 2)] == GOLD["derived"]


def test_config_matches_js():
    assert CONFIG.dt == GOLD["config"]["dt"]
    assert CONFIG.warmup == GOLD["config"]["warmup"]
    assert CONFIG.max_sim_time == GOLD["config"]["maxSimTime"]
    assert CONFIG.density == GOLD["config"]["density"]


def test_network_matches_js():
    net = build_network()
    assert [n.id for n in net.node_list] == [n["id"] for n in GOLD["nodes"]]
    for n, g in zip(net.node_list, GOLD["nodes"]):
        assert (n.x, n.y, n.type) == (g["x"], g["y"], g["type"])
    assert net.boundary_ids == GOLD["boundaryIds"]
    assert [s.id for s in net.segments] == [s["id"] for s in GOLD["segments"]]
    assert [e.id for e in net.edges] == [e["id"] for e in GOLD["edges"]]
    for e, g in zip(net.edges, GOLD["edges"]):
        assert e.length == pytest.approx(g["length"]) and e.seg_id == g["segId"]
    assert [e.id for e in net.entry_edges] == GOLD["entryEdges"]


def test_routes_match_js():
    net = build_network()
    assert [e.id for e in find_route(net, "W0", "E1")] == GOLD["route_W0_E1"]
    assert [e.id for e in find_route(net, "W0", "E1", {"J00|J01"})] == GOLD["route_W0_E1_blocked_J00_J01"]


def test_route_edge_cases():
    net = build_network()
    assert find_route(net, "W0", "W0") is None
    assert find_route(net, "W0", "E1", {"W0|J00"}) is None  # the only way in is blocked
    assert find_route(net, "W0", "NOPE") is None


def test_demand_matches_js_vehicle_for_vehicle():
    net = build_network()
    cars, amb = generate_demand(net, Scenario(seed=7, density="medium"))
    assert len(cars) == len(GOLD["vehicles"]) == 365
    for c, g in zip(cars, GOLD["vehicles"]):
        assert c.depart == pytest.approx(g["t"], abs=1e-9)
        assert (c.origin, c.dest) == (g["from"], g["to"])
        assert c.edges == g["edges"]
        assert c.speed_factor == pytest.approx(g["speedFactor"], abs=1e-9)
    assert amb.depart == 60.0 and amb.edges == GOLD["route_W0_E1"]


# ---- demand behaviour ----
def test_demand_deterministic_and_seed_sensitive():
    net = build_network()
    a, _ = generate_demand(net, Scenario(seed=7))
    b, _ = generate_demand(net, Scenario(seed=7))
    c, _ = generate_demand(net, Scenario(seed=8))
    assert [(v.depart, v.edges) for v in a] == [(v.depart, v.edges) for v in b]
    assert [(v.depart, v.edges) for v in a] != [(v.depart, v.edges) for v in c]


def test_demand_rate_is_plausible():
    net = build_network()
    for dens in ("low", "medium", "high"):
        cars, _ = generate_demand(net, Scenario(seed=11, density=dens))
        expected = entry_rate(Scenario(density=dens)) * 10 * CONFIG.max_sim_time / 3600
        assert abs(len(cars) - expected) < 0.2 * expected, (dens, len(cars), expected)


def test_custom_rate_clamped_like_js():
    assert entry_rate(Scenario(density="custom", rate=5000)) == 900
    assert entry_rate(Scenario(density="custom", rate=10)) == 100
    assert entry_rate(Scenario(density="custom", rate="abc")) == 330


def test_blocked_entry_segment_spawns_nothing_there():
    net = build_network()
    cars, _ = generate_demand(net, Scenario(seed=3, blocked=("W0|J00",)))
    assert all(not (c.origin == "W0") for c in cars)


# ---- SUMO network ----
def test_sumo_network_structure():
    import sumolib
    net, path = build_static_network()
    sn = sumolib.net.readNet(path)
    tls = sorted(t.getID() for t in sn.getTrafficLights())
    assert tls == sorted(net.junction_ids)  # only the 6 junctions are signalised
    assert len(sn.getEdges()) == 34
    for e in net.edges:
        se = sn.getEdge(sumo_edge_id(e.id))
        (x1, y1), (x2, y2) = se.getFromNode().getCoord(), se.getToNode().getCoord()
        assert math.hypot(x2 - x1, y2 - y1) == pytest.approx(e.length, abs=0.01)
        assert se.getLength() == pytest.approx(e.length, abs=18)  # junction cut-outs shorten lanes slightly


# ---- SUMO runs ----
def test_sumo_runs_clean_and_ambulance_arrives():
    r = run_smoke(Scenario(seed=7, density="medium"), label="t_clean")
    assert r["ambulance_depart"] is not None and r["ambulance_depart"] >= 60.0
    assert r["ambulance_arrive"] is not None, r
    assert r["collisions"] == 0 and r["teleports"] == 0
    assert len(r["signalised_junctions"]) == 6


def test_sumo_deterministic_same_seed_and_different_seed():
    a = run_smoke(Scenario(seed=7), until=150, stop_when_amb_arrives=False, label="t_a")
    b = run_smoke(Scenario(seed=7), until=150, stop_when_amb_arrives=False, label="t_b")
    c = run_smoke(Scenario(seed=8), until=150, stop_when_amb_arrives=False, label="t_c")
    assert a["routes_sha1"] == b["routes_sha1"] != c["routes_sha1"]
    assert a["arrival_fingerprint"] == b["arrival_fingerprint"]
    assert a["arrival_fingerprint"] != c["arrival_fingerprint"]


def test_sumo_handles_blocked_route():
    r = run_smoke(Scenario(seed=7, blocked=("J11|J12",)), label="t_blk")
    assert r["ambulance_arrive"] is not None and r["collisions"] == 0

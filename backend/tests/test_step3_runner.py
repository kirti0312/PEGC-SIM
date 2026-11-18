"""Step 3: run_scenario() wiring, tested WITHOUT SUMO against a small fake TraCI connection.

The fake is not a traffic simulator. It moves the ambulance along its route at a fixed speed, stops it at a
red light, and keeps one stopped car on every approach. It only exists to execute the real runner loop
(subscriptions, ambulance tracking, per-axis queues, safety check, log sink, audit) so wiring mistakes show
up here instead of in Codespaces. Real SUMO behaviour is checked by the SUMO tests in test_step3.py.
"""
import sys
import types

import pytest

import pegc_sumo.runner as runner
from pegc_sumo.config import CONFIG, Scenario
from pegc_sumo.network import build_network, sumo_edge_id

VAR_SPEED, VAR_ROAD_ID, VAR_LANEPOSITION = 0x40, 0x50, 0x56
INTERNAL_LEN = 10.0
AMB_SPEED = 12.0


class _Phase:
    def __init__(self, state):
        self.state = state


class _Logic:
    def __init__(self, phases):
        self.phases = phases


class FakeTrafficLight:
    def __init__(self, net):
        self.net = net
        self.set = {}
        self.links = {}
        self.logic = {}
        for jid in net.junction_ids:
            incoming = [e for e in net.edges if e.to == jid]
            groups, axes = [], []
            for e in incoming:
                axis = "H" if net.nodes[e.frm].y == net.nodes[e.to].y else "V"
                for _ in range(3):
                    groups.append([(sumo_edge_id(e.id) + "_0", "out_0", "via")])
                    axes.append(axis)
            self.links[jid] = groups
            self.axes = axes
            gh = "".join("G" if a == "H" else "r" for a in axes)
            gv = "".join("G" if a == "V" else "r" for a in axes)
            self.logic[jid] = [_Logic([_Phase(gh), _Phase(gh.replace("G", "y")), _Phase(gv), _Phase(gv.replace("G", "y"))])]
            self.set[jid] = gh

    def getControlledLinks(self, jid):
        return self.links[jid]

    def getAllProgramLogics(self, jid):
        return self.logic[jid]

    def setRedYellowGreenState(self, jid, state):
        self.set[jid] = state

    def getRedYellowGreenState(self, jid):
        return self.set[jid]

    def is_green_for(self, jid, edge):
        for i, grp in enumerate(self.links[jid]):
            if grp[0][0] == edge + "_0":
                return self.set[jid][i] in "Gg"
        return True


class FakeConn:
    """Minimal TraCI stand-in. Attribute groups mirror conn.trafficlight / conn.simulation / conn.vehicle / conn.lane."""

    def __init__(self, net, amb_edges):
        self.net = net
        self.t = 0.0
        self.route = [sumo_edge_id(e) for e in amb_edges]
        self.len = {}
        for e in net.edges:
            boundary = net.nodes[e.frm].type == "boundary" or net.nodes[e.to].type == "boundary"
            self.len[sumo_edge_id(e.id)] = 172.8 if boundary else 205.6
        self.trafficlight = FakeTrafficLight(net)
        self.simulation = types.SimpleNamespace(getTime=lambda: self.t,
                                                getDepartedIDList=lambda: list(self._departed),
                                                getArrivedIDList=lambda: list(self._arrived))
        self.vehicle = types.SimpleNamespace(subscribe=self._subscribe, getAllSubscriptionResults=self._results,
                                             getLanePosition=self._lane_pos, getSpeed=self._speed)
        self.lane = types.SimpleNamespace(getLength=lambda lane: self.len[lane.rsplit("_", 1)[0]],
                                          getLastStepVehicleIDs=self._lane_vehicles)
        self.cars = {}          # stopped queue cars: id -> (edge, pos)
        for e in net.edges:
            if net.nodes[e.to].type == "junction":
                sid = sumo_edge_id(e.id)
                self.cars["c_" + sid] = (sid, self.len[sid] - 8.0)
        self.amb = None         # [route index, road, pos, speed]
        self.amb_k = 0
        self._departed, self._arrived = [], []
        self.subs = {}          # vehicle id -> (variables, step number when subscribed)
        self.step_no = 0
        self.closed = False

    # ---- vehicles -----------------------------------------------------------------
    def _subscribe(self, vid, variables):
        self.subs[vid] = (list(variables), self.step_no)

    def _state(self, vid):
        if vid == "amb0" and self.amb is not None:
            return self.amb["road"], self.amb["pos"], self.amb["speed"]
        if vid in self.cars:
            edge, pos = self.cars[vid]
            return edge, pos, 0.0
        return None

    def _results(self):
        out = {}
        for vid, (variables, since) in self.subs.items():
            st = self._state(vid)
            if st is None or since >= self.step_no:      # real TraCI only reports from the step after subscribing
                continue
            values = {VAR_ROAD_ID: st[0], VAR_LANEPOSITION: st[1], VAR_SPEED: st[2]}
            out[vid] = {v: values[v] for v in variables}
        return out

    def _lane_pos(self, vid):
        return self._state(vid)[1]

    def _speed(self, vid):
        return self._state(vid)[2]

    def _lane_vehicles(self, lane):
        edge = lane.rsplit("_", 1)[0]
        ids = [v for v, (e, _) in self.cars.items() if e == edge]
        if self.amb is not None and self.amb["road"] == edge:
            ids.append("amb0")
        return ids

    # ---- time ---------------------------------------------------------------------
    def simulationStep(self):
        self._departed, self._arrived = [], []
        dt = CONFIG.dt
        if self.step_no == 0:
            self._departed += list(self.cars)
        if self.amb is None and abs(self.t - CONFIG.warmup) < 1e-9:
            self.amb = {"road": self.route[0], "pos": 0.0, "speed": AMB_SPEED}
            self.amb_k = 0
            self._departed.append("amb0")
        elif self.amb is not None:
            a = self.amb
            if a["road"].startswith(":"):                                   # crossing a junction
                a["pos"] += AMB_SPEED * dt
                if a["pos"] >= INTERNAL_LEN:
                    self.amb_k += 1
                    a.update(road=self.route[self.amb_k], pos=0.0, speed=AMB_SPEED)
            else:
                edge, length = a["road"], self.len[a["road"]]
                junction = self.net.edge_by_id[self._browser(edge)].to
                ends_at_junction = self.net.nodes[junction].type == "junction"
                nxt = a["pos"] + AMB_SPEED * dt
                if ends_at_junction and nxt >= length - 2.0 and not self.trafficlight.is_green_for(junction, edge):
                    a["pos"], a["speed"] = max(a["pos"], length - 2.0), 0.0   # red light: wait at the stop line
                else:
                    a["pos"], a["speed"] = nxt, AMB_SPEED
                    if a["pos"] >= length:
                        if not ends_at_junction or self.amb_k == len(self.route) - 1:
                            self._arrived.append("amb0")
                            self.amb = None
                            self.amb_k = -1
                        else:
                            a.update(road=f":{junction}_0_0", pos=0.0)
        self.t = round(self.t + dt, 6)
        self.step_no += 1

    @staticmethod
    def _browser(sumo_edge):
        return sumo_edge.replace("_to_", ">")

    def close(self):
        self.closed = True


@pytest.fixture
def fake_sumo(monkeypatch):
    """Patch the runner so run_scenario drives a FakeConn instead of SUMO. Returns the list of created connections."""
    constants = types.ModuleType("traci.constants")
    constants.VAR_SPEED, constants.VAR_ROAD_ID, constants.VAR_LANEPOSITION = VAR_SPEED, VAR_ROAD_ID, VAR_LANEPOSITION
    traci = types.ModuleType("traci")
    traci.constants = constants
    monkeypatch.setitem(sys.modules, "traci", traci)
    monkeypatch.setitem(sys.modules, "traci.constants", constants)
    net = build_network()
    created = []

    def fake_start(net_path, routes_path, seed, label, cfg=CONFIG):
        conn = FakeConn(net, files["amb"].edges)
        created.append(conn)
        return conn

    real_build = runner.build_scenario_files
    files = {}

    def build(net_, s, cfg=CONFIG, out_dir=None):
        files.update(real_build(net_, s, cfg, out_dir))
        return files

    monkeypatch.setattr(runner, "build_static_network", lambda cfg=CONFIG, out_dir=None: (net, "unused.net.xml"))
    monkeypatch.setattr(runner, "build_scenario_files", build)
    monkeypatch.setattr(runner, "start_sumo", fake_start)
    return created


def test_runner_strategy_a_with_fake_sumo(fake_sumo):
    r = runner.run_scenario(Scenario(seed=7, density="low"), "A", label="fake_a")
    conn = fake_sumo[0]
    assert conn.closed and conn.step_no == int(CONFIG.max_sim_time / CONFIG.dt)
    assert r.metrics["ambFinished"] and r.metrics["violations"] == 0
    assert [e["kind"] for e in r.log] == ["enter", "pass", "arrive"]          # A never logs a decision
    assert all(n == 0 for n in r.safety["byRule"].values())
    assert r.safety["requests"]["switched"] == r.switches > 50
    assert r.safety["requests"]["delayed"] == 0                               # A only asks when the plan allows it
    # only the ambulance is subscribed to its lane position
    assert conn.subs["amb0"][0] == [VAR_SPEED, VAR_ROAD_ID, VAR_LANEPOSITION]
    assert all(v[0] == [VAR_SPEED, VAR_ROAD_ID] for k, v in conn.subs.items() if k != "amb0")


def test_runner_strategy_b_with_fake_sumo_logs_requests_and_stays_safe(fake_sumo):
    r = runner.run_scenario(Scenario(seed=7, density="low"), "B", label="fake_b")
    assert r.metrics["ambFinished"] and r.metrics["violations"] == 0
    assert all(n == 0 for n in r.safety["byRule"].values())
    kinds = [e["kind"] for e in r.log]
    assert kinds[0] == "enter" and kinds[-1] == "arrive" and "request" in kinds and "release" in kinds
    requests = [e for e in r.log if e["kind"] == "request"]
    releases = [e for e in r.log if e["kind"] == "release"]
    assert len(requests) == len(releases) >= 3                                # one request and one release per junction passed
    for e in requests:
        assert e["data"]["junction"] in e["text"] and "requesting" in e["text"] and "(ignores cross traffic)" in e["text"]
        assert 115.0 <= e["data"]["dStop"] <= CONFIG.b_trigger_distance       # first step inside 120 m (<= 3 m per step at 12 m/s)
        assert e["data"]["nQueue"] == 1                                       # the stopped car ahead on that approach
        assert e["data"]["eta"] > 0
    # every request is later released, in time order
    times = [e["t"] for e in r.log]
    assert times == sorted(times)
    assert r.safety["requests"]["switched"] == r.switches


def test_runner_a_and_b_share_signals_until_the_ambulance_is_near(fake_sumo):
    a = runner.run_scenario(Scenario(seed=7, density="low"), "A", label="fake_a2")
    b = runner.run_scenario(Scenario(seed=7, density="low"), "B", label="fake_b2")
    first_request = min(e["t"] for e in b.log if e["kind"] == "request")
    assert first_request > CONFIG.warmup
    assert [s for s in a.series if s[0] <= CONFIG.warmup] == [s for s in b.series if s[0] <= CONFIG.warmup]


def test_runner_rejects_unbuilt_strategies(fake_sumo):
    with pytest.raises(NotImplementedError):
        runner.run_scenario(Scenario(seed=7, density="low"), "C", label="fake_c")

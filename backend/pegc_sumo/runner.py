"""Run one scenario in SUMO under one strategy and return metrics, log and audit results."""
from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Callable

from .ambinfo import AmbState, AmbTracker
from .config import CONFIG, Config, Scenario
from .frame import FrameBuilder
from .metrics import MetricsRecorder
from .network import sumo_edge_id
from .signals import SignalBank
from .strategies import ApproachData, JunctionData, OutData, make_strategy
from .sumo_io import build_scenario_files, build_static_network, start_sumo


@dataclass
class RunContext:
    """What a strategy sees each step. Strategies may only REQUEST changes through ctx.bank."""
    cfg: Config
    bank: SignalBank
    t: float = 0.0
    amb: AmbTracker | None = None                       # ambulance position / upcoming junctions
    queue_ahead: Callable | None = None                 # queue_ahead(Upcoming) -> queued vehicles ahead
    log: Callable | None = None                         # log(t, junction, kind, text, **data)
    junction_data: Callable | None = None               # junction_data(jid) -> strategies.JunctionData (Strategy C)
    recovery_time: float | None = None                  # set once the queues have recovered (Strategy C)
    total_queued: int = 0                               # vehicles currently queued at all junction approaches

    def log_event(self, junction, kind: str, text: str, **data) -> None:
        if self.log is not None:
            self.log(self.t, junction, kind, text, **data)


@dataclass
class RunResult:
    strategy: str
    scenario: Scenario
    metrics: dict
    log: list
    series: list
    safety: dict
    switches: int
    wall_seconds: float
    routes_sha1: str = ""
    extra: dict = field(default_factory=dict)


def run_scenario(s: Scenario, strategy: str = "A", label: str | None = None, cfg: Config = CONFIG,
                 on_step: Callable | None = None) -> RunResult:
    """on_step (optional, default None = old behaviour): called after every step with a small snapshot dict."""
    import traci.constants as tc

    strat = make_strategy(strategy)
    net, net_path = build_static_network(cfg)
    files = build_scenario_files(net, s, cfg)
    amb = files["amb"]
    conn = start_sumo(net_path, files["routes"], s.seed, label or f"run_{strategy}_{s.seed}_{id(s) % 100000}", cfg)
    t0 = _time.time()
    try:
        bank = SignalBank(conn, net, cfg)
        bank.apply(force=True)
        rec = MetricsRecorder(net, cfg, amb_route_edges=amb.edges if amb else None)
        amb_id = amb.id if amb else None

        # real SUMO lane lengths of the ambulance route (they differ from the browser's 220 m / 180 m)
        tracker = None
        if amb:
            lengths = {sumo_edge_id(e): conn.lane.getLength(sumo_edge_id(e) + "_0") for e in amb.edges}
            tracker = AmbTracker(net, amb.edges, cfg, lengths)

        def queue_ahead(u) -> int:
            """Queued vehicles ahead of the ambulance on the approach it is about to use (speed below queue_speed)."""
            n = 0
            for vid in conn.lane.getLastStepVehicleIDs(u.edge + "_0"):
                if vid.startswith("amb"):
                    continue
                if conn.vehicle.getLanePosition(vid) > u.from_pos and conn.vehicle.getSpeed(vid) < cfg.queue_speed:
                    n += 1
            return n

        def log(t, junction, kind, text, **data):
            rec.event(t, kind, text, junction=junction, **data)

        # safety-layer decisions (delays, forced switches, hold-cap releases) go to the log as they happen
        bank.event_sink = lambda t_ev, jid, kind, text, data: rec.event(t_ev, kind, text, junction=jid, **data)
        ctx = RunContext(cfg, bank, 0.0, tracker, queue_ahead, log)
        # approach edge -> (junction, axis), for the per-axis queue counts the safety layer uses
        approach = {}
        for e in net.edges:
            if net.nodes[e.to].type == "junction":
                approach[sumo_edge_id(e.id)] = (e.to, "H" if net.nodes[e.frm].y == net.nodes[e.to].y else "V")

        # --- junction data for Strategy C (read lazily from SUMO, cached for one step) ---
        approach_edges = {}     # junction -> {axis: [sumo edge ids that end at it]}
        out_edges = {}          # junction -> {axis: [sumo edge ids that start at it]}
        for e in net.edges:
            axis = "H" if net.nodes[e.frm].y == net.nodes[e.to].y else "V"
            if net.nodes[e.to].type == "junction":
                approach_edges.setdefault(e.to, {"H": [], "V": []})[axis].append(sumo_edge_id(e.id))
            if net.nodes[e.frm].type == "junction":
                out_edges.setdefault(e.frm, {"H": [], "V": []})[axis].append(sumo_edge_id(e.id))
        jd_cache: dict = {"t": None, "data": {}}
        len_cache: dict = {}

        def lane_len(edge: str) -> float:
            if edge not in len_cache:
                len_cache[edge] = conn.lane.getLength(edge + "_0")
            return len_cache[edge]

        def junction_data(jid: str) -> JunctionData:
            if jd_cache["t"] != ctx.t:
                jd_cache["t"], jd_cache["data"] = ctx.t, {}
            if jid in jd_cache["data"]:
                return jd_cache["data"][jid]
            d = JunctionData()
            for axis in ("H", "V"):
                for edge in approach_edges.get(jid, {}).get(axis, []):
                    L = lane_len(edge)
                    a = ApproachData()
                    for vid in conn.lane.getLastStepVehicleIDs(edge + "_0"):
                        speed = conn.vehicle.getSpeed(vid)
                        streak = rec.streak.get(vid, 0.0)
                        a.veh.append((L - conn.vehicle.getLanePosition(vid), speed, streak))
                        if speed < cfg.queue_speed:
                            a.queue += 1
                            a.queue_streak = max(a.queue_streak, streak)
                    d.approach[axis].append(a)
                for edge in out_edges.get(jid, {}).get(axis, []):
                    d.out[axis].append(OutData(lane_len(edge), [conn.vehicle.getLength(v)
                                                                for v in conn.lane.getLastStepVehicleIDs(edge + "_0")]))
            jd_cache["data"][jid] = d
            return d

        ctx.junction_data = junction_data

        # Step 9a: browser-format vehicle frames, built only for the steps the sampler asks for
        builder = FrameBuilder(net, cfg, lambda e: lane_len(e), lambda vid: list(conn.vehicle.getRoute(vid))) if on_step else None
        frame_wanted = getattr(on_step, "frame_wanted", None)
        last_speed: dict = {}
        log_i = 0

        steps = int(round(cfg.max_sim_time / cfg.dt))
        for k in range(steps):
            ctx.t = k * cfg.dt
            bank.update()
            strat.control(ctx)
            bank.check_safety(ctx.t)
            bank.apply()
            conn.simulationStep()
            t_end = round(conn.simulation.getTime(), 6)
            departed = list(conn.simulation.getDepartedIDList())
            arrived = list(conn.simulation.getArrivedIDList())
            for vid in departed:
                watched = [tc.VAR_SPEED, tc.VAR_ROAD_ID]
                if vid == amb_id or on_step is not None:
                    watched.append(tc.VAR_LANEPOSITION)            # the ambulance (and, for live frames, every vehicle)
                conn.vehicle.subscribe(vid, watched)
            res = conn.vehicle.getAllSubscriptionResults()
            vehicles = {vid: (r[tc.VAR_SPEED], r[tc.VAR_ROAD_ID]) for vid, r in res.items() if tc.VAR_SPEED in r}
            if tracker is not None:
                r = res.get(amb_id)
                if r is not None and tc.VAR_LANEPOSITION in r:
                    tracker.update(AmbState(r[tc.VAR_ROAD_ID], r[tc.VAR_LANEPOSITION], r[tc.VAR_SPEED]))
                else:
                    tracker.update(None)
            qj = {jid: {"H": 0, "V": 0} for jid in bank.signals}
            for speed, road in vehicles.values():
                ja = approach.get(road)
                if ja is not None and speed < cfg.queue_speed:
                    qj[ja[0]][ja[1]] += 1
            bank.queue_by_junction = qj                           # used by the safety layer in the next step
            bank.audit()
            rec.on_step(t_end, departed, arrived, vehicles)
            ctx.recovery_time = rec.recovery_time                 # read by Strategy C in the next step
            ctx.total_queued = rec.total_queued
            if on_step is not None:
                st = tracker.state if tracker is not None else None
                amb_snap = None if st is None else {"road": st.road, "pos": st.pos, "speed": st.speed}
                snap = {"t": t_end, "queued": rec.total_queued, "vehicles": len(vehicles),
                        "signals": {j: {"phase": sg.phase, "green_axis": sg.green_axis, "timer": sg.timer}
                                    for j, sg in bank.signals.items()},
                        "amb": amb_snap, "log": rec.log[log_i:]}
                log_i = len(rec.log)
                if frame_wanted is None or frame_wanted():
                    snap["veh"] = builder.frame(((vid, r[tc.VAR_ROAD_ID], r[tc.VAR_LANEPOSITION], r[tc.VAR_SPEED])
                                                 for vid, r in res.items()
                                                 if tc.VAR_SPEED in r and tc.VAR_LANEPOSITION in r), last_speed)
                    if amb_snap is not None:
                        p = builder.pose(amb_id, st.road, st.pos)
                        if p is not None:
                            amb_snap["edge"], amb_snap["s"], amb_snap["prev"] = p[0], round(p[1], 2), p[2]
                last_speed = {vid: v[0] for vid, v in vehicles.items()}
                on_step(snap)
        m = rec.metrics()
        m["violations"] = bank.violations
        safety = {"violations": bank.violations, "byRule": dict(bank.by_rule), "requests": dict(bank.request_stats)}
        return RunResult(strategy, s, m, rec.log, rec.series, safety,
                         sum(sg.switches for sg in bank.signals.values()),
                         round(_time.time() - t0, 2), files["sha1"])
    finally:
        conn.close()

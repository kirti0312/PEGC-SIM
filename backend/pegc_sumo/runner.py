"""Run one scenario in SUMO under one strategy and return metrics, log and audit results."""
from __future__ import annotations

import time as _time
from dataclasses import dataclass, field

from .config import CONFIG, Config, Scenario
from .metrics import MetricsRecorder
from .network import sumo_edge_id
from .signals import SignalBank
from .strategies import make_strategy
from .sumo_io import build_scenario_files, build_static_network, start_sumo


@dataclass
class RunContext:
    cfg: Config
    bank: SignalBank
    t: float = 0.0


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


def run_scenario(s: Scenario, strategy: str = "A", label: str | None = None, cfg: Config = CONFIG) -> RunResult:
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
        ctx = RunContext(cfg, bank)
        watched = {tc.VAR_SPEED, tc.VAR_ROAD_ID}
        steps = int(round(cfg.max_sim_time / cfg.dt))
        for k in range(steps):
            ctx.t = k * cfg.dt
            bank.update()
            strat.control(ctx)
            bank.apply()
            conn.simulationStep()
            t_end = round(conn.simulation.getTime(), 6)
            departed = list(conn.simulation.getDepartedIDList())
            arrived = list(conn.simulation.getArrivedIDList())
            for vid in departed:
                conn.vehicle.subscribe(vid, [tc.VAR_SPEED, tc.VAR_ROAD_ID])
            res = conn.vehicle.getAllSubscriptionResults()
            vehicles = {vid: (r[tc.VAR_SPEED], r[tc.VAR_ROAD_ID]) for vid, r in res.items() if tc.VAR_SPEED in r}
            bank.audit()
            rec.on_step(t_end, departed, arrived, vehicles)
        m = rec.metrics()
        m["violations"] = bank.violations
        return RunResult(strategy, s, m, rec.log, rec.series,
                         {"violations": bank.violations, "byRule": dict(bank.by_rule)},
                         sum(sg.switches for sg in bank.signals.values()),
                         round(_time.time() - t0, 2), files["sha1"])
    finally:
        conn.close()

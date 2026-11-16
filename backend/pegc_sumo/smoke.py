"""Step 1 smoke run: load the network and demand into SUMO and report what happened.

    python -m pegc_sumo.smoke --seed 7 --density medium
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time as _time

from .config import CONFIG, Scenario
from .sumo_io import build_scenario_files, build_static_network, start_sumo


def run_smoke(s: Scenario, until: float | None = None, stop_when_amb_arrives: bool = True, label: str = "smoke"):
    net, net_path = build_static_network()
    files = build_scenario_files(net, s)
    conn = start_sumo(net_path, files["routes"], s.seed, label)
    out = {"seed": s.seed, "density": s.density, "routes_sha1": files["sha1"],
           "vehicles_planned": len(files["cars"]) + (1 if files["amb"] else 0)}
    departed = arrived = collisions = teleports = max_in_net = 0
    amb_depart = amb_arrive = None
    fp = hashlib.sha1()
    horizon = until if until is not None else CONFIG.max_sim_time
    try:
        out["signalised_junctions"] = sorted(conn.trafficlight.getIDList())
        phases = conn.trafficlight.getAllProgramLogics(out["signalised_junctions"][0])[0].phases
        out["tls_program_phases"] = [(p.duration, p.state) for p in phases]
        t0 = _time.time()
        while conn.simulation.getTime() < horizon - 1e-9:
            conn.simulationStep()
            now = round(conn.simulation.getTime(), 3)
            dep = conn.simulation.getDepartedIDList()
            arr = conn.simulation.getArrivedIDList()
            departed += len(dep)
            arrived += len(arr)
            collisions += conn.simulation.getCollidingVehiclesNumber()
            teleports += conn.simulation.getStartingTeleportNumber()
            max_in_net = max(max_in_net, conn.vehicle.getIDCount())
            if "amb0" in dep:
                amb_depart = now
            for v in arr:
                fp.update(f"{v}@{now}".encode())
                if v == "amb0":
                    amb_arrive = now
            if stop_when_amb_arrives and amb_arrive is not None:
                break
        out["wall_seconds"] = round(_time.time() - t0, 2)
        out["sim_time_end"] = round(conn.simulation.getTime(), 2)
        out["pending_insertions_end"] = len(conn.simulation.getPendingVehicles()) if hasattr(conn.simulation, "getPendingVehicles") else None
    finally:
        conn.close()
    out.update({"departed": departed, "arrived": arrived, "collisions": collisions, "teleports": teleports,
                "max_vehicles_in_network": max_in_net, "ambulance_depart": amb_depart,
                "ambulance_arrive": amb_arrive,
                "ambulance_travel_time": None if amb_arrive is None or amb_depart is None else round(amb_arrive - amb_depart, 2),
                "arrival_fingerprint": fp.hexdigest()})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--density", default="medium", choices=["low", "medium", "high"])
    ap.add_argument("--origin", default="W0")
    ap.add_argument("--dest", default="E1")
    ap.add_argument("--full", action="store_true", help="run the full 420 s instead of stopping when the ambulance arrives")
    a = ap.parse_args()
    s = Scenario(origin=a.origin, dest=a.dest, density=a.density, seed=a.seed)
    print(json.dumps(run_smoke(s, stop_when_amb_arrives=not a.full), indent=1))


if __name__ == "__main__":
    main()

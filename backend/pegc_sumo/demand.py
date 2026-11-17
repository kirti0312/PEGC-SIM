"""Seeded demand generation that reproduces the browser engine's vehicle stream.

Draw order matches Simulation._spawn / _makeOrdinary in src/engine/sim.js:
  per step, per entry edge: one spawn draw; on a hit, rngVeh draws speed jitter, colour, then
  up to 6 destination picks. A vehicle's depart time is the simulation time at the start of
  the step in which the browser engine created it.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import CONFIG, Config, Scenario, entry_rate
from .network import Network, find_route, sumo_edge_id
from .rng import Rng, derive_seed


@dataclass
class VehicleSpec:
    id: str
    kind: str            # "car" | "amb"
    depart: float
    origin: str
    dest: str
    edges: list          # browser edge ids, e.g. "W0>J00"
    speed_factor: float


def generate_demand(net: Network, s: Scenario, cfg: Config = CONFIG):
    """Return (vehicles sorted by depart time, ambulance spec or None)."""
    blocked = set(s.blocked)
    rng_spawn = Rng(derive_seed(s.seed, 1))
    rng_veh = Rng(derive_seed(s.seed, 2))
    p = entry_rate(s, cfg) / 3600.0 * cfg.dt
    steps = int(round(cfg.max_sim_time / cfg.dt))
    cars: list[VehicleSpec] = []
    n = 0
    for k in range(steps):
        t = k * cfg.dt
        for e in net.entry_edges:
            draw = rng_spawn.next()
            if e.seg_id in blocked or not draw < p:
                continue
            jitter = rng_veh.range(-cfg.speed_jitter, cfg.speed_jitter)
            vmax = cfg.car_max_speed * (1 + jitter)
            rng_veh.int(6)  # colour draw, kept so the stream stays aligned
            options = [b for b in net.boundary_ids if b != e.frm]
            route = None
            dest = None
            for _ in range(6):
                if route:
                    break
                dest = rng_veh.pick(options)
                route = find_route(net, e.frm, dest, blocked)
            if not route:
                continue
            cars.append(VehicleSpec(f"c{n:05d}", "car", round(t, 3), e.frm, route[-1].to,
                                    [x.id for x in route], vmax / cfg.car_max_speed))
            n += 1
    amb = None
    route = find_route(net, s.origin, s.dest, blocked)
    if route:
        amb = VehicleSpec("amb0", "amb", cfg.warmup, s.origin, s.dest,
                          [x.id for x in route], cfg.amb_max_speed / cfg.car_max_speed)
    return cars, amb


def generate_ambulances(net: Network, s: Scenario, cfg: Config = CONFIG) -> list:
    """One VehicleSpec per ambulance in the scenario (ids amb0, amb1, ...). Unroutable ones are skipped."""
    blocked = set(s.blocked)
    out = []
    for i, a in enumerate(s.ambulance_specs(cfg)):
        route = find_route(net, a.origin, a.dest, blocked)
        if route:
            out.append(VehicleSpec(a.id if s.ambulances else f"amb{i}", "amb", a.start_t, a.origin, a.dest,
                                   [x.id for x in route], cfg.amb_max_speed / cfg.car_max_speed))
    return out


def write_routes(cars, amb, out_path: str, cfg: Config = CONFIG, extra_ambs=()) -> str:
    items = [(v.depart, 1, v) for v in cars]
    if amb:
        items.append((amb.depart, 0, amb))  # ambulance first among simultaneous departures
    for a in extra_ambs:
        items.append((a.depart, 0, a))
    items.sort(key=lambda x: (x[0], x[1]))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write('<routes>\n')
        f.write(f'  <vType id="car" vClass="passenger" length="{cfg.car_length}" minGap="{cfg.min_gap}" '
                f'maxSpeed="20" accel="{cfg.accel}" decel="{cfg.decel}" emergencyDecel="{cfg.hard_decel}" '
                f'tau="{cfg.time_headway}" sigma="0" guiShape="passenger"/>\n')
        f.write(f'  <vType id="amb" vClass="emergency" length="{cfg.amb_length}" minGap="{cfg.min_gap}" '
                f'maxSpeed="{cfg.amb_max_speed}" accel="{cfg.amb_accel}" decel="{cfg.decel}" '
                f'emergencyDecel="{cfg.hard_decel}" tau="{cfg.time_headway}" sigma="0" '
                f'guiShape="emergency" color="1,1,1"/>\n')
        for _, _, v in items:
            edges = " ".join(sumo_edge_id(e) for e in v.edges)
            f.write(f'  <vehicle id="{v.id}" type="{v.kind}" depart="{v.depart:.2f}" departLane="best" '
                    f'departSpeed="max" speedFactor="{v.speed_factor:.6f}">\n'
                    f'    <route edges="{edges}"/>\n  </vehicle>\n')
        f.write('</routes>\n')
    return out_path

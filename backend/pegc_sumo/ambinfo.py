"""Ambulance information for strategies (rule-based, no machine learning).

Port of Simulation.ambInfo() and etaFree() from the browser engine, adapted to SUMO geometry:
  * SUMO lanes end AT the stop line, so the distance to a junction's stop line is simply
    (lane length - lane position) plus the lengths of the edges in between. The browser engine
    measures to the junction centre and subtracts its 12 m "junctionHalf"; SUMO needs no such step.
  * SUMO lane lengths (205.6 m and 172.8 m) are shorter than the browser's 220 m and 180 m, because
    the junctions have a real size. Pass the real lane lengths in `lengths`.

Pure Python (no SUMO import) so it can be unit-tested with synthetic states.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import CONFIG, Config
from .network import Network, sumo_edge_id


def eta_free(v: float, D: float, cfg: Config = CONFIG) -> float:
    """Rule-based (kinematic) free-flow arrival time over distance D. Exact port of etaFree()."""
    if D <= 0:
        return 0.0
    a = cfg.amb_accel
    vm = min(cfg.amb_max_speed, cfg.c_eta_cruise)
    v0 = min(v, vm)
    da = (vm * vm - v0 * v0) / (2 * a)
    if D <= da:
        return (-v0 + math.sqrt(v0 * v0 + 2 * a * D)) / a
    return (vm - v0) / a + (D - da) / vm


def free_flow_time(D: float, cfg: Config = CONFIG) -> float:
    """Idealised unobstructed travel time over distance D (accelerate at amb_accel to amb_max_speed, then cruise).
    Used only to express a multi-ambulance run's delay; it ignores curves and junction geometry."""
    if D <= 0:
        return 0.0
    vm, a = cfg.amb_max_speed, cfg.amb_accel
    da = vm * vm / (2 * a)
    return math.sqrt(2 * D / a) if D <= da else vm / a + (D - da) / vm


@dataclass
class AmbState:
    """What SUMO reports about the ambulance at the end of a step."""
    road: str       # SUMO edge id, or an internal junction lane such as ":J00_3_0"
    pos: float      # position along its lane (m)
    speed: float    # m/s


@dataclass
class Upcoming:
    """One junction ahead of the ambulance on its route (same fields as the browser engine's `upcoming`)."""
    junction: str
    axis: str            # axis of the approach the ambulance uses ("H" or "V")
    edge: str            # SUMO edge id of that approach
    k: int               # index of the edge in the route
    d_stop: float        # distance to the stop line (m)
    eta: float           # rule-based free-flow arrival time at the stop line (s)
    from_pos: float      # queue counting starts after this lane position (-1: whole edge)
    n_queue: int | None = None   # queued vehicles ahead; filled in lazily by whoever needs it


class AmbTracker:
    def __init__(self, net: Network, route_edges: list, cfg: Config = CONFIG, lengths: dict | None = None):
        """route_edges: browser edge ids such as "W0>J00" (VehicleSpec.edges).
        lengths: SUMO edge id -> lane length. If omitted the browser lengths are used (only for tests)."""
        self.cfg = cfg
        self.route = [sumo_edge_id(e) for e in route_edges]
        self.index = {e: i for i, e in enumerate(self.route)}
        self.len = {}
        self.junction = {}   # route index -> junction id entered at the end of that edge
        self.axis = {}
        for i, bid in enumerate(route_edges):
            e = net.edge_by_id[bid]
            sid = self.route[i]
            self.len[i] = float(lengths[sid]) if lengths and sid in lengths else e.length
            if net.nodes[e.to].type == "junction":
                self.junction[i] = e.to
                self.axis[i] = "H" if net.nodes[e.frm].y == net.nodes[e.to].y else "V"
        self.state: AmbState | None = None
        self.ri = -1          # index of the last route edge the ambulance was on

    def update(self, state: AmbState | None) -> None:
        """Call once per step with the ambulance state, or None when it is not (or no longer) in the network."""
        self.state = state
        if state is not None and state.road in self.index:
            self.ri = self.index[state.road]

    def upcoming(self) -> list | None:
        """Junctions still ahead, nearest first. None while the ambulance is not in the network."""
        st = self.state
        if st is None or self.ri < 0:
            return None
        on_edge = st.road in self.index
        inside_junction = st.road.startswith(":")
        if not on_edge and not inside_junction:
            return None
        out = []
        if on_edge:
            k0, d_end, from_pos = self.ri, self.len[self.ri] - st.pos, st.pos
        else:                                   # on a junction's internal lane: that junction is already passed
            k0, d_end, from_pos = self.ri + 1, 0.0, -1.0
        for k in range(k0, len(self.route)):
            if k > k0 or not on_edge:
                d_end += self.len[k]
            if k not in self.junction:
                continue
            d_stop = d_end
            if d_stop < -0.5:
                continue
            out.append(Upcoming(self.junction[k], self.axis[k], self.route[k], k, d_stop,
                                eta_free(st.speed, max(0.0, d_stop), self.cfg),
                                from_pos if k == k0 and on_edge else -1.0))
        return out

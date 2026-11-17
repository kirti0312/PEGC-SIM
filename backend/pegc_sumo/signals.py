"""Signal state machine (port of src/engine/signals.js) plus the TraCI link mapping.

JunctionSignal is pure Python (no SUMO) so the plan can be unit-tested.
SignalBank owns one JunctionSignal per junction and writes their states into SUMO.
Only the safety layer may start a switch (Step 3 replaces the placeholder in SignalBank.request).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import CONFIG, Config
from .network import Network, sumo_edge_id


def other(axis: str) -> str:
    return "V" if axis == "H" else "H"


@dataclass
class JunctionSignal:
    id: str
    green_axis: str            # axis that is green (or was green, during Y / R)
    next_axis: str
    phase: str                 # "G" green | "Y" yellow | "R" all-red clearance
    timer: float
    switches: int = 0
    last_green_dur: float | None = None

    @staticmethod
    def make(junction_id: str, idx: int, cfg: Config = CONFIG) -> "JunctionSignal":
        r, c = int(junction_id[1]), int(junction_id[2])
        axis = "H" if (r + c) % 2 == 0 else "V"
        return JunctionSignal(junction_id, axis, other(axis), "G", (idx * 7) % cfg.fixed_green)

    def update(self, cfg: Config = CONFIG) -> bool:
        """Advance one time step. Returns True if the displayed state changed."""
        self.timer += cfg.dt
        if self.phase == "Y" and self.timer >= cfg.yellow - 1e-9:
            self.phase, self.timer = "R", 0.0
            return True
        if self.phase == "R" and self.timer >= cfg.all_red - 1e-9:
            self.phase, self.green_axis, self.timer = "G", self.next_axis, 0.0
            return True
        return False

    def start_switch(self) -> None:
        self.last_green_dur = self.timer
        self.next_axis = other(self.green_axis)
        self.phase, self.timer = "Y", 0.0
        self.switches += 1

    def light_for(self, axis: str) -> str:
        if self.green_axis != axis:
            return "R"
        return {"G": "G", "Y": "Y", "R": "R"}[self.phase]


def edge_axis(net: Network, sumo_lane_or_edge: str) -> str:
    """Axis of the road a link comes from: 'H' if it runs west-east, else 'V'."""
    edge = sumo_lane_or_edge.rsplit("_", 1)[0] if sumo_lane_or_edge.rsplit("_", 1)[-1].isdigit() else sumo_lane_or_edge
    for e in net.edges:
        if sumo_edge_id(e.id) == edge:
            return "H" if net.nodes[e.frm].y == net.nodes[e.to].y else "V"
    raise KeyError(f"unknown edge {edge}")


def link_states_from_program(phases, link_axes: list) -> dict:
    """Build green/yellow/all-red strings per axis from SUMO's own green phases.

    SUMO's generated program already knows which links may use green ('G') or yield ('g'),
    so we reuse its green phases instead of inventing link states that could conflict.
    """
    n = len(link_axes)
    green: dict = {}
    for ph in phases:
        st = ph.state
        if "y" in st or not any(ch in "Gg" for ch in st):
            continue
        axes = {link_axes[i] for i, ch in enumerate(st) if ch in "Gg"}
        if len(axes) == 1:
            green.setdefault(axes.pop(), st)
    if set(green) != {"H", "V"}:
        raise RuntimeError("SUMO's signal program does not have one green phase per axis; got " + repr([p.state for p in phases]))
    yellow = {a: "".join("y" if ch in "Gg" else "r" for ch in st) for a, st in green.items()}
    return {"green": green, "yellow": yellow, "red": "r" * n, "n": n}


@dataclass
class SignalBank:
    conn: object
    net: Network
    cfg: Config = CONFIG
    signals: dict = field(default_factory=dict)
    states: dict = field(default_factory=dict)        # junction -> strings
    applied: dict = field(default_factory=dict)       # junction -> last string written
    violations: int = 0
    by_rule: dict = field(default_factory=lambda: {"conflict": 0, "mismatch": 0, "min_green": 0})
    switch_log: list = field(default_factory=list)

    def __post_init__(self):
        for idx, jid in enumerate(self.net.junction_ids):
            self.signals[jid] = JunctionSignal.make(jid, idx, self.cfg)
            links = self.conn.trafficlight.getControlledLinks(jid)
            link_axes = [edge_axis(self.net, grp[0][0]) for grp in links]
            logic = self.conn.trafficlight.getAllProgramLogics(jid)[0]
            self.states[jid] = link_states_from_program(logic.phases, link_axes)
            self.states[jid]["link_axes"] = link_axes

    def intended_state(self, jid: str) -> str:
        sig, st = self.signals[jid], self.states[jid]
        if sig.phase == "G":
            return st["green"][sig.green_axis]
        if sig.phase == "Y":
            return st["yellow"][sig.green_axis]
        return st["red"]

    def apply(self, force: bool = False) -> None:
        for jid in self.signals:
            s = self.intended_state(jid)
            if force or self.applied.get(jid) != s:
                self.conn.trafficlight.setRedYellowGreenState(jid, s)
                self.applied[jid] = s

    def update(self) -> None:
        for sig in self.signals.values():
            sig.update(self.cfg)

    def request(self, jid: str, want_axis: str, t: float = 0.0) -> bool:
        """PLACEHOLDER safety rule for Step 2: switch only from green and only after minimum green.
        Step 3 replaces this with the full safety layer (hold limits, wait cap, lockout, logging)."""
        sig = self.signals[jid]
        if want_axis == sig.green_axis or sig.phase != "G":
            return False
        if sig.timer + 1e-9 < self.cfg.min_green:
            return False
        sig.start_switch()
        self.switch_log.append((round(t, 3), jid, sig.next_axis))
        return True

    def audit(self) -> None:
        """Check what SUMO actually shows: never two axes green, and SUMO must not override our state."""
        for jid in self.signals:
            actual = self.conn.trafficlight.getRedYellowGreenState(jid)
            self.audit_state(jid, actual)

    def audit_state(self, jid: str, actual: str) -> None:
        axes = self.states[jid]["link_axes"]
        green_axes = {axes[i] for i, ch in enumerate(actual) if ch in "Gg"}
        if len(green_axes) > 1:
            self.violations += 1
            self.by_rule["conflict"] += 1
        if actual != self.applied.get(jid):
            self.violations += 1
            self.by_rule["mismatch"] += 1

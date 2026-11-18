"""Safety layer (port of src/engine/safety.js).

Every strategy request passes through apply_request(). Strategies can only ASK; this module decides
whether and when a phase change really happens. check_safety() is an independent per-step verifier.
The target for both is zero violations.

Pure Python: no SUMO import, so every rule can be unit-tested with synthetic inputs.
SignalBank (signals.py) stays the only writer of signal states into SUMO.

Rules, in the order apply_request applies them (same order as the browser engine):
  1. hold cap     emergency hold against the normal plan is limited to max_emergency_hold s per junction
  2. max wait     no approach may wait longer than max_wait s; this overrides emergency requests
  3. min green    a switch needs max(min_green, lockout) s of green first
Yellow (3 s) and all-red (2 s) are fixed by the signal state machine and checked by check_safety().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from .config import CONFIG, Config
from .signals import JunctionSignal, other
from .textfmt import js_fixed

# every rule name that can appear in a violation count ("mismatch" comes from the SUMO state audit)
RULES = ("conflict", "mismatch", "min_green", "yellow", "all_red", "hold_cap", "max_wait")

AxisQueueFn = Callable[[str, str], int]   # (junction id, axis) -> queued vehicles on that axis's approaches


@dataclass(frozen=True)
class Request:
    """What a strategy asks for at one junction in one step."""
    desired: str                      # axis the strategy wants green: "H" or "V"
    emergency: bool = False           # True if the request exists because of the ambulance
    baseline_desired: str | None = None   # what the normal fixed plan would want (defaults to desired)
    rec: dict | None = None           # optional strategy info (used by recovery logging in Step 4)

    @property
    def baseline(self) -> str:
        return self.desired if self.baseline_desired is None else self.baseline_desired


@dataclass
class SafetyResult:
    outcome: str                      # "accepted" | "switched" | "delayed" | "rejected"
    reason: str | None = None         # rule that shaped the outcome (see below), or None
    switched: bool = False
    delayed: bool = False
    delay_remaining: float = 0.0
    events: list = field(default_factory=list)   # [{"kind", "text", "data"}] for the decision log

    # outcome meanings
    #   rejected  a transition (yellow / all-red) is already in progress; reason "transition"
    #   switched  the safety layer started green -> yellow; reason may be "max_wait" if it forced it
    #   delayed   a switch was wanted but minimum green / lockout has not elapsed; reason "min_green"
    #   accepted  nothing to change (keeping the current green); reason may be "hold_cap" if released


@dataclass
class SafetyCounter:
    violations: int = 0
    by_rule: dict = field(default_factory=lambda: {r: 0 for r in RULES})
    details: list = field(default_factory=list)

    def add(self, rule: str, where: str, t: float) -> None:
        self.violations += 1
        self.by_rule[rule] = self.by_rule.get(rule, 0) + 1
        if len(self.details) < 50:
            self.details.append({"rule": rule, "where": where, "t": t})


def apply_request(sig: JunctionSignal, req: Request, t: float, cfg: Config = CONFIG,
                  axis_queue: AxisQueueFn | None = None) -> SafetyResult:
    """Decide what a strategy request really does. May start a switch (sig.start_switch)."""
    if sig.phase != "G":
        return SafetyResult("rejected", "transition")   # a transition is already in progress

    res = SafetyResult("accepted")
    want = req.desired
    cur = sig.green_axis
    oth = other(cur)

    # 1. Emergency hold cap: holding green against the normal plan is limited per junction.
    if req.emergency and want == cur and req.baseline != cur:
        if sig.emergency_used >= cfg.max_emergency_hold - 1e-9:
            want = req.baseline
            res.reason = "hold_cap"
            res.events.append({
                "kind": "safety",
                "text": f"Safety layer: emergency hold at {sig.id} reached the {_num(cfg.max_emergency_hold)} s cap; releasing to normal plan.",
                "data": {"rule": "holdCap", "used": sig.emergency_used},
            })
        else:
            sig.emergency_used += cfg.dt

    # 2. No approach may wait longer than max_wait (overrides emergency requests).
    last_end = sig.last_green_end.get(oth)
    waited = t - (t if last_end is None else last_end)
    limit = cfg.max_wait - cfg.yellow - cfg.all_red - 1
    queued_other = axis_queue(sig.id, oth) if axis_queue else 0
    if want == cur and waited >= limit and queued_other > 0 and sig.timer >= cfg.min_green:
        want = oth
        res.reason = "max_wait"
        res.events.append({
            "kind": "safety",
            "text": f"Safety layer: {oth}-axis approach at {sig.id} has waited {js_fixed(waited, 0)} s; forcing a switch (overrides emergency requests).",
            "data": {"rule": "maxWait", "waited": waited},
        })

    # 3. Minimum green / lockout before any switch.
    if want != cur:
        need = max(cfg.min_green, cfg.lockout)
        if sig.timer + 1e-9 >= need:
            sig.start_switch(t)
            res.outcome, res.switched = "switched", True
        else:
            res.outcome, res.delayed = "delayed", True
            res.reason = "min_green"
            res.delay_remaining = need - sig.timer
            if req.emergency and not sig.delay_logged:
                sig.delay_logged = True
                res.events.append({
                    "kind": "safety",
                    "text": f"Safety layer delayed the switch at {sig.id} by {js_fixed(res.delay_remaining, 1)} s (minimum green {_num(cfg.min_green)} s).",
                    "data": {"rule": "minGreen", "remaining": res.delay_remaining},
                })
    return res


def _num(x: float) -> str:
    """Format a config number the way a JS template string would (7 -> "7", 2.5 -> "2.5")."""
    return str(int(x)) if float(x).is_integer() else str(x)


def check_safety(signals: Iterable[JunctionSignal], t: float, cfg: Config = CONFIG,
                 counter: SafetyCounter | None = None, axis_queue: AxisQueueFn | None = None) -> SafetyCounter:
    """Independent per-step verification of the signal state. Violations are counted, never fixed."""
    counter = counter if counter is not None else SafetyCounter()
    for sig in signals:
        if sig.light_for("H") != "R" and sig.light_for("V") != "R":
            counter.add("conflict", sig.id, t)
        if sig.last_switch_green_dur is not None:
            if sig.last_switch_green_dur + 1e-9 < cfg.min_green:
                counter.add("min_green", sig.id, t)
            sig.last_switch_green_dur = None
        if sig.last_yellow_dur is not None:
            if abs(sig.last_yellow_dur - cfg.yellow) > 1e-6:
                counter.add("yellow", sig.id, t)
            sig.last_yellow_dur = None
        if sig.last_red_dur is not None:
            if abs(sig.last_red_dur - cfg.all_red) > 1e-6:
                counter.add("all_red", sig.id, t)
            sig.last_red_dur = None
        if sig.emergency_used > cfg.max_emergency_hold + cfg.dt + 1e-9:
            counter.add("hold_cap", sig.id, t)
        for axis in ("H", "V"):
            lg = sig.last_green_end.get(axis)
            if lg is None or sig.light_for(axis) != "R":
                continue
            q = axis_queue(sig.id, axis) if axis_queue else 0
            if t - lg > cfg.max_wait and q > 0 and not sig.wait_latch[axis]:
                sig.wait_latch[axis] = True
                counter.add("max_wait", sig.id, t)
    return counter

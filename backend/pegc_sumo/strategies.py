"""Signal strategies. Each strategy only REQUESTS changes through the signal bank (and so through the
safety layer); it never starts a switch itself.

Step 2: Strategy A (fixed-time).
Step 3: Strategy B (basic priority).
Step 4: Strategy C (PEGC) and its ablations C1 (no queue awareness) and C2 (no recovery).

Ports of fixedReq(), strategyA(), strategyB(), timing(), evaluate(), recoveryReq() and strategyC() from
frontend/src/engine/strategies.js. The arithmetic keeps the JavaScript order of operations so results match
the browser engine exactly (tests/golden_strategy_c.json.gz).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .safety import Request
from .signals import other
from .textfmt import js_fixed


def fixed_req(sig, cfg) -> Request:
    """Normal fixed-time plan, used by A and by B whenever it is not acting on the ambulance."""
    want = other(sig.green_axis) if sig.timer >= cfg.fixed_green else sig.green_axis
    return Request(desired=want, emergency=False, baseline_desired=want)


class StrategyA:
    id = "A"

    def control(self, ctx) -> None:
        cfg = ctx.cfg
        for jid, sig in ctx.bank.signals.items():
            ctx.bank.request(jid, fixed_req(sig, cfg), ctx.t)


class StrategyB:
    """Basic priority: when the ambulance is within b_trigger_distance of the next junction's stop line,
    request that junction's ambulance axis. Cross traffic is ignored; the safety layer still decides."""
    id = "B"

    def control(self, ctx) -> None:
        cfg = ctx.cfg
        info = ctx.amb.upcoming() if ctx.amb is not None else None
        first = info[0] if info else None
        for jid, sig in ctx.bank.signals.items():
            base = fixed_req(sig, cfg)
            if first is not None and first.junction == jid and first.d_stop <= cfg.b_trigger_distance:
                if not sig.b_active:
                    sig.b_active = True
                    nq = first.n_queue
                    if nq is None:
                        nq = ctx.queue_ahead(first) if ctx.queue_ahead is not None else 0
                    first.n_queue = nq
                    ctx.log_event(
                        jid, "request",
                        f"{jid}: ambulance {js_fixed(first.d_stop, 0)} m from the stop line, ETA {js_fixed(first.eta)} s, "
                        f"queue ahead {nq} → requesting {first.axis}-axis green now (ignores cross traffic).",
                        dStop=first.d_stop, eta=first.eta, nQueue=nq)
                ctx.bank.request(jid, Request(desired=first.axis, emergency=True, baseline_desired=base.desired), ctx.t)
            else:
                if sig.b_active:
                    sig.b_active = False
                    ctx.log_event(jid, "release", f"{jid}: ambulance request released; back to the fixed plan.")
                ctx.bank.request(jid, base, ctx.t)


def _js_num(x: float) -> str:
    """Number as JavaScript prints it in a template string (60.0 -> "60")."""
    return str(int(x)) if float(x).is_integer() else repr(float(x))


# ---------------------------------------------------------------------------------------------
# Strategy C (PEGC). Rule-based and cost-based; no machine learning.
# ---------------------------------------------------------------------------------------------
@dataclass
class ApproachData:
    """One approach edge of a junction. veh = [(d, speed, wait_streak)] with d = metres to the stop line."""
    queue: int = 0
    queue_streak: float = 0.0
    veh: list = field(default_factory=list)


@dataclass
class OutData:
    """One outgoing edge: its length and the lengths of the vehicles on it."""
    length: float = 1.0
    veh_lengths: list = field(default_factory=list)


@dataclass
class JunctionData:
    """Everything evaluate() and recovery_req() need to know about one junction (per axis)."""
    approach: dict = field(default_factory=lambda: {"H": [], "V": []})   # axis -> [ApproachData]
    out: dict = field(default_factory=lambda: {"H": [], "V": []})        # axis -> [OutData]


def pressure(data: JunctionData, axis: str) -> float:
    p = 0
    for e in data.approach[axis]:
        p += e.queue + 0.1 * e.queue_streak
    return p


def cross_stats(data: JunctionData, axis: str) -> tuple:
    """(cars, longest wait streak) on the cross street: queued cars plus moving cars within 50 m of the line."""
    cars = 0
    longest = 0
    for e in data.approach[axis]:
        cars += e.queue
        for d, v, streak in e.veh:
            if d >= 0 and d < 50 and v >= 1.0:
                cars += 1
            if streak > longest and d >= 0:
                longest = streak
    return cars, longest


def out_occupancy(data: JunctionData, axis: str, cfg) -> float:
    occ = 0
    for e in data.out[axis]:
        used = 0
        for ln in e.veh_lengths:
            used += ln + cfg.min_gap
        occ = max(occ, used / e.length)
    return occ


@dataclass
class Timing:
    green_now: bool
    t_green_base: float
    t_green_act: float
    t_end_base: float = 0.0
    min_rem: float = 0.0
    fixed_seq: bool = False


def timing(sig, A: str, cfg) -> Timing:
    """Time until the ambulance's axis (A) can be green, under the normal plan and if PEGC acts."""
    Y, R, fg = cfg.yellow, cfg.all_red, cfg.fixed_green
    mg = max(cfg.min_green, cfg.lockout)
    if sig.green_axis == A:
        if sig.phase == "G":
            return Timing(True, 0, 0, t_end_base=max(0, fg - sig.timer), min_rem=0)
        if sig.phase == "Y":
            rem = Y - sig.timer
            return Timing(False, rem + R + fg + Y + R, rem + R + mg + Y + R, fixed_seq=True)
        rem = R - sig.timer
        return Timing(False, rem + fg + Y + R, rem + mg + Y + R, fixed_seq=True)
    if sig.phase == "G":
        min_rem = max(0, mg - sig.timer)
        return Timing(False, max(0, fg - sig.timer) + Y + R, min_rem + Y + R, min_rem=min_rem)
    r = Y - sig.timer + R if sig.phase == "Y" else R - sig.timer
    return Timing(False, r, r, fixed_seq=True)


def evaluate(ctx, sig, u, extra: float, queue_aware: bool):
    """Decide what to request at the junction `u` the ambulance is approaching. Returns (request, predicted_pass, eta)."""
    cfg = ctx.cfg
    A = u.axis
    O = other(A)
    base = fixed_req(sig, cfg)
    if u.n_queue is None:                       # queue ahead is read lazily, only when a decision needs it
        u.n_queue = ctx.queue_ahead(u) if ctx.queue_ahead is not None else 0
    n = u.n_queue if queue_aware else 0
    h = cfg.c_discharge_headway
    eta = u.eta + extra
    Y, R, fg = cfg.yellow, cfg.all_red, cfg.fixed_green
    tm = timing(sig, A, cfg)

    t_start_in = 0
    if tm.green_now:
        t_need = max(eta, n * h)
        t_pass_act = t_need
        if t_need + 1 <= tm.t_end_base:
            delay_saved = 0
            lost_green = 0
            t_pass_base = t_need
        else:
            t_again = tm.t_end_base + 2 * (Y + R) + fg
            t_pass_base = max(eta, t_again + n * h)
            delay_saved = t_pass_base - t_need
            lost_green = max(0, t_need + 1 - tm.t_end_base)
        lead = n * h
    else:
        t_pass_base = max(eta, tm.t_green_base + n * h)
        t_pass_act = max(eta, tm.t_green_act + n * h)
        delay_saved = t_pass_base - t_pass_act
        lost_green = max(0, tm.t_green_base - tm.t_green_act)
        lead = tm.t_green_act + n * h
        if tm.fixed_seq:
            t_start_in = max(0, eta - n * h - tm.t_green_act)
        else:
            latest = eta - n * h - (Y + R)
            t_start_in = 0 if latest <= tm.min_rem else latest

    data = ctx.junction_data(sig.id)
    cars, longest = cross_stats(data, O)
    occ = out_occupancy(data, A, cfg)
    benefit = cfg.c_weight_amb_delay * delay_saved
    spill = cfg.c_spillback_penalty * 20 * max(0, occ - 0.5)
    cost = cfg.c_cost_per_car * cars * lost_green + cfg.c_wait_weight * longest + spill

    if sig.c_mode == "active":
        mode = "active"                         # sticky until the ambulance has passed
    elif delay_saved < cfg.c_min_delay_saved:
        mode = "idle"
    elif benefit <= cost:
        mode = "declined"
    else:
        mode = "prepare" if t_start_in > 0.75 else "active"

    info = {"eta": u.eta, "n": u.n_queue, "nUsed": n, "lead": lead, "delaySaved": delay_saved, "benefit": benefit,
            "cost": cost, "crossCars": cars, "tStartIn": t_start_in, "mode": mode}
    axis_name = f"{A}-axis"
    if mode != "idle" and sig.c_last_logged != mode:
        sig.c_last_logged = mode
        head = f"{sig.id}: ambulance ETA {js_fixed(u.eta)} s, queue ahead {u.n_queue}, lead time {js_fixed(lead)} s"
        tail = (f"est. delay saved {js_fixed(delay_saved)} s (benefit {js_fixed(benefit, 0)}) "
                f"vs cross-street cost {js_fixed(cost, 0)} ({cars} cars)")
        if mode == "prepare":
            text = f"{head} → preparing: switch to {axis_name} green in {js_fixed(t_start_in)} s; {tail}."
        elif mode == "declined":
            text = f"{head} → not interrupting cross traffic; cost exceeds benefit; {tail}."
        elif tm.green_now:
            text = f"{head} → holding {axis_name} green; {tail}."
        else:
            text = f"{head} → switching to {axis_name} green now; {tail}."
        kind = "decline" if mode == "declined" else "prepare" if mode == "prepare" else "hold" if tm.green_now else "switch"
        ctx.log_event(sig.id, kind, text, **info)
    if mode not in ("active", "prepare") and sig.c_last_logged == "prepare":
        sig.c_last_logged = ""
    sig.c_mode = mode

    req = Request(desired=A, emergency=True, baseline_desired=base.desired) if mode == "active" else base
    return req, (t_pass_act if mode in ("active", "prepare") else t_pass_base), eta


def recovery_req(ctx, sig) -> Request:
    """After the ambulance has passed: serve the busier axis until queues return to normal."""
    cfg = ctx.cfg
    data = ctx.junction_data(sig.id)
    cur = sig.green_axis
    oth = other(cur)
    pc = pressure(data, cur)
    po = pressure(data, oth)
    if pc == 0 and po == 0:
        want = fixed_req(sig, cfg).desired
    elif po > pc + 1:
        want = oth
    elif sig.timer >= cfg.fixed_green * 2:
        want = oth
    else:
        want = cur
    return Request(desired=want, emergency=False, baseline_desired=want, rec={"pc": pc, "po": po, "cur": cur, "oth": oth})


class StrategyC:
    """PEGC: predict the ambulance's arrival at the next junctions, weigh the ambulance's time saved against
    the cost to cross traffic and spillback, then REQUEST the switch through the safety layer. After the
    ambulance has passed, a recovery phase serves the busier axis. C1 and C2 are the ablations."""

    def __init__(self, strategy_id: str = "C", queue_aware: bool = True, recovery: bool = True):
        self.id = strategy_id
        self.queue_aware = queue_aware
        self.recovery = recovery
        self._prev: list = []                   # junctions that were ahead of the ambulance in the previous step

    def _note_passes(self, ctx, info) -> None:
        """Same bookkeeping as Simulation._onAmbPass: a junction that is no longer ahead has been cleared."""
        now = [u.junction for u in info] if info is not None else []
        for jid in self._prev:
            if jid not in now:
                sig = ctx.bank.signals[jid]
                sig.amb_passed_at = ctx.t
                sig.c_mode = "passed"
                sig.c_recovering = True
                sig.b_active = False
                ctx.log_event(jid, "pass", f"Ambulance cleared {jid} at t = {js_fixed(ctx.t)} s.", at=ctx.t)
        self._prev = now

    def control(self, ctx) -> None:
        cfg = ctx.cfg
        info = ctx.amb.upcoming() if ctx.amb is not None else None
        self._note_passes(ctx, info)
        reqs = {}
        if info:
            extra = 0
            for u in info[:cfg.c_lookahead_junctions]:
                sig = ctx.bank.signals[u.junction]
                req, predicted_pass, eta = evaluate(ctx, sig, u, extra, self.queue_aware)
                reqs[u.junction] = req
                extra = max(0, predicted_pass - eta) + extra
        for jid, sig in ctx.bank.signals.items():
            req = reqs.get(jid)
            if req is None:
                if self.recovery and sig.c_recovering:
                    if ctx.recovery_time is not None or ctx.t - sig.amb_passed_at > cfg.c_recovery_max:
                        sig.c_recovering = False
                        why = ("queues are back near the pre-ambulance level" if ctx.recovery_time is not None
                               else f"{_js_num(cfg.c_recovery_max)} s recovery window ended")
                        ctx.log_event(jid, "recovery", f"{jid}: recovery finished ({why}); back to the fixed plan.",
                                      queued=ctx.total_queued)
                        req = fixed_req(sig, cfg)
                    else:
                        req = recovery_req(ctx, sig)
                else:
                    req = fixed_req(sig, cfg)
            res = ctx.bank.request(jid, req, ctx.t)
            if res.switched and req.rec:
                r = req.rec
                ctx.log_event(jid, "recovery",
                              f"{jid}: recovery after the ambulance → serving {r['oth']}-axis approach first "
                              f"(queue/wait pressure {js_fixed(r['po'])} vs {js_fixed(r['pc'])}).",
                              po=r["po"], pc=r["pc"])


def make_strategy(strategy_id: str):
    if strategy_id == "A":
        return StrategyA()
    if strategy_id == "B":
        return StrategyB()
    if strategy_id == "C":
        return StrategyC("C", queue_aware=True, recovery=True)
    if strategy_id == "C1":
        return StrategyC("C1", queue_aware=False, recovery=True)       # ablation: no queue awareness
    if strategy_id == "C2":
        return StrategyC("C2", queue_aware=True, recovery=False)       # ablation: no recovery
    raise ValueError(f"Unknown strategy {strategy_id}")

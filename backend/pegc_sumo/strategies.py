"""Signal strategies. Each strategy only REQUESTS changes through the signal bank (and so through the
safety layer); it never starts a switch itself.

Step 2: Strategy A (fixed-time).
Step 3: Strategy B (basic priority). C, C1 and C2 arrive in Step 4.

Ports of fixedReq(), strategyA() and strategyB() from frontend/src/engine/strategies.js.
"""
from __future__ import annotations

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


def make_strategy(strategy_id: str):
    if strategy_id == "A":
        return StrategyA()
    if strategy_id == "B":
        return StrategyB()
    raise NotImplementedError(f"Strategy {strategy_id} is built in a later step (C/C1/C2: Step 4).")

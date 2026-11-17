"""Signal strategies. Each strategy only REQUESTS changes through the signal bank.

Step 2: Strategy A (fixed-time) only. B arrives in Step 3 and C, C1, C2 in Step 4.
"""
from __future__ import annotations

from .signals import other


class StrategyA:
    id = "A"

    def control(self, ctx) -> None:
        cfg = ctx.cfg
        for jid, sig in ctx.bank.signals.items():
            want = other(sig.green_axis) if sig.timer >= cfg.fixed_green else sig.green_axis
            ctx.bank.request(jid, want, ctx.t)


def make_strategy(strategy_id: str):
    if strategy_id == "A":
        return StrategyA()
    raise NotImplementedError(f"Strategy {strategy_id} is built in a later step (B: Step 3, C/C1/C2: Step 4).")

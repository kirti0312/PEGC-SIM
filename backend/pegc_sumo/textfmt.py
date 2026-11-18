"""Number formatting that matches JavaScript's Number.toFixed, so Python log lines can be
compared with the browser engine's log lines character for character.

Python's format() rounds exact ties to the even digit (86.5 -> "86"); JavaScript's toFixed rounds
them up (86.5 -> "87"). Times in this project are multiples of 0.25, so ties do occur.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def js_fixed(x: float, digits: int = 1) -> str:
    q = Decimal(1).scaleb(-digits)
    return str(Decimal(float(x)).quantize(q, rounding=ROUND_HALF_UP))

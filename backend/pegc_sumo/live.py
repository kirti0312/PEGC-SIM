"""Step 10: commands sent to a run WHILE it is running (pure Python, no SUMO, no web code).

The API puts commands here from its own threads; the runner takes them at the start of each simulation step.
A run that received any command is marked `modified` and must not be used in clean comparisons.
"""
from __future__ import annotations

import threading
import time
from collections import deque

MAX_AMBULANCES = 3


class LiveError(ValueError):
    """The command cannot be accepted (the API turns this into HTTP 409 or 422)."""


class LiveControl:
    def __init__(self, n_ambulances: int = 1, speed: float | None = None, max_ambulances: int = MAX_AMBULANCES):
        """speed: simulated seconds per wall second (None = as fast as SUMO runs, used by tests). A live run is
        paced so that a person can react to what they see; SUMO alone would finish in a few seconds."""
        self.speed = speed
        self.max_ambulances = max_ambulances
        self.amb_total = n_ambulances          # ambulances the run has had or has been told to get
        self.modified = False
        self.finished = False
        self.cancelled = False                 # set by cancel(): the runner stops at the next step
        self.t = 0.0                           # latest simulated time, set by the runner
        self.events: list = []                 # what was applied, with the simulated time
        self._q: deque = deque()
        self._lock = threading.Lock()
        self._wall0: float | None = None

    # ---- called from the API side ----
    def set_traffic(self, rate: float) -> None:
        with self._lock:
            self._check_open()
            self._q.append(("traffic", float(rate)))
            self.modified = True

    def dispatch(self, origin: str, dest: str) -> str:
        """Queue an ambulance; returns the id it will get."""
        with self._lock:
            self._check_open()
            if self.amb_total >= self.max_ambulances:
                raise LiveError(f"at most {self.max_ambulances} ambulances per run")
            aid = f"amb{self.amb_total}"
            self.amb_total += 1
            self._q.append(("dispatch", aid, origin, dest))
            self.modified = True
            return aid

    def _check_open(self) -> None:
        if self.finished:
            raise LiveError("the run has already finished")

    # ---- called from the runner ----
    def drain(self) -> list:
        with self._lock:
            out = list(self._q)
            self._q.clear()
            return out

    def record(self, t: float, kind: str, **data) -> None:
        with self._lock:
            self.events.append({"t": round(t, 3), "kind": kind, **data})

    def pace(self, sim_t: float) -> None:
        """Sleep so that simulated time does not run ahead of speed x wall time."""
        self.t = sim_t
        if not self.speed:
            return
        now = time.monotonic()
        if self._wall0 is None:
            self._wall0 = now
        wait = self._wall0 + sim_t / self.speed - now
        if wait > 0:
            time.sleep(min(wait, 1.0))

    def cancel(self) -> None:
        """Stop the run (the front end left it, e.g. Restart). No further commands are accepted."""
        with self._lock:
            self.cancelled = True
            self.finished = True
            self._q.clear()

    def close(self) -> None:
        with self._lock:
            self.finished = True
            self._q.clear()

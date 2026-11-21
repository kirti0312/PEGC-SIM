"""Step 8 logic without any web code: input rules, calibrated traffic levels, one-at-a-time run queue."""
from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field, replace

from experiments.batch import rate_for
from pegc_sumo.config import CONFIG, AmbulanceSpec, Scenario
from pegc_sumo.live import LiveControl, LiveError, MAX_AMBULANCES
from pegc_sumo.network import build_network, find_route

HERE = os.path.dirname(os.path.abspath(__file__))
EXPERIMENTS = os.path.join(os.path.dirname(HERE), "experiments")
CALIBRATION_PATH = os.path.join(EXPERIMENTS, "calibration.json")
C3_TUNING_PATH = os.path.join(EXPERIMENTS, "c3_tuning.json")

STRATEGIES = ("A", "B", "C", "C1", "C2", "C3")
COMPARE_STRATEGIES = ("A", "B", "C")
DENSITIES = ("low", "medium", "high", "custom")
ENGINE = "SUMO"
COMPARE_KEYS = ("ambTravelTime", "avgWait", "longestWait")


class InputError(ValueError):
    """Bad scenario input (the API turns it into HTTP 422)."""


class Busy(RuntimeError):
    """Run queue is full (HTTP 429)."""


def _net():
    if not hasattr(_net, "n"):
        _net.n = build_network()
    return _net.n


def boundary_nodes() -> list:
    return list(_net().boundary_ids)


def load_calibration(path: str = CALIBRATION_PATH) -> dict:
    """Returns {'scale': float|None, 'status': str}. Never invents a scale."""
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {"scale": None, "status": "calibration.json missing or unreadable"}
    sc = d.get("scale")
    if not isinstance(sc, (int, float)) or isinstance(sc, bool) or not sc > 0:
        return {"scale": None, "status": f"no usable scale (status={d.get('status')})"}
    return {"scale": float(sc), "status": str(d.get("status", "ok"))}


def load_c3(path: str = C3_TUNING_PATH) -> dict:
    """C3 weight from c3_tuning.json if present, else the config default."""
    try:
        ch = json.load(open(path, encoding="utf-8"))["choice"]
        return {"weight": float(ch["weight"]), "source": "c3_tuning.json", "status": ch.get("status")}
    except (OSError, ValueError, KeyError, TypeError):
        return {"weight": float(CONFIG.c3_wait_weight), "source": "config default", "status": None}


def _check_amb_route(net, origin, dest, blocked, what: str = "ambulance") -> None:
    if origin not in net.boundary_ids or dest not in net.boundary_ids:
        raise InputError(f"{what}: origin and dest must be boundary nodes: {', '.join(net.boundary_ids)}")
    if origin == dest:
        raise InputError(f"{what}: origin and dest must differ")
    if find_route(net, origin, dest, frozenset(blocked)) is None:
        raise InputError(f"{what}: blocked segments leave no valid route")


def traffic_rate(density, rate, cal: dict) -> tuple:
    """(veh/h per entry, density) for a live traffic change: a preset (calibrated) or a raw custom rate."""
    if density is not None and rate is not None:
        raise InputError("give either density or rate, not both")
    if density is None and rate is None:
        raise InputError("give density (low, medium, high) or rate")
    if rate is not None:
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not (CONFIG.rate_min <= rate <= CONFIG.rate_max):
            raise InputError(f"rate must be {CONFIG.rate_min} to {CONFIG.rate_max} veh/h per entry")
        return int(round(rate)), "custom"
    if density not in ("low", "medium", "high"):
        raise InputError("density must be low, medium or high")
    if cal["scale"] is None:
        raise InputError(f"presets need a calibration scale: {cal['status']}")
    return rate_for(density, cal["scale"]), density


def resolve(origin: str, dest: str, density: str, rate, seed, blocked, cal: dict, ambulances=None) -> tuple:
    """Validate and build (Scenario, meta). Presets use calibrated rates; custom is a raw veh/h per entry.
    ambulances (Step 10, optional): list of {origin, dest, start} (1 to 3). Without it behaviour is exactly as before."""
    net = _net()
    if origin not in net.boundary_ids or dest not in net.boundary_ids:
        raise InputError(f"origin and dest must be boundary nodes: {', '.join(net.boundary_ids)}")
    if origin == dest:
        raise InputError("origin and dest must differ")
    if density not in DENSITIES:
        raise InputError(f"density must be one of {', '.join(DENSITIES)}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise InputError("seed must be an integer")
    segs = {e.seg_id for e in net.edges}
    blocked = tuple(dict.fromkeys(blocked or ()))
    bad = [b for b in blocked if b not in segs]
    if bad:
        raise InputError(f"unknown segment(s): {', '.join(bad)}")
    if find_route(net, origin, dest, frozenset(blocked)) is None:
        raise InputError("blocked segments leave no valid route")
    amb_specs = ()
    if ambulances:
        if len(ambulances) > MAX_AMBULANCES:
            raise InputError(f"at most {MAX_AMBULANCES} ambulances")
        for i, a in enumerate(ambulances):
            _check_amb_route(net, a["origin"], a["dest"], blocked, f"ambulance {i}")
            st = a.get("start")
            if st is not None and (isinstance(st, bool) or not isinstance(st, (int, float)) or not (0 <= st <= 300)):
                raise InputError(f"ambulance {i}: start must be 0 to 300 seconds")
        origin, dest = ambulances[0]["origin"], ambulances[0]["dest"]
        if len(ambulances) > 1 or ambulances[0].get("start") is not None:
            amb_specs = tuple(AmbulanceSpec(f"amb{i}", a["origin"], a["dest"],
                                            None if a.get("start") is None else float(a["start"]))
                              for i, a in enumerate(ambulances))
    if density == "custom":
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not (CONFIG.rate_min <= rate <= CONFIG.rate_max):
            raise InputError(f"custom rate must be {CONFIG.rate_min} to {CONFIG.rate_max} veh/h per entry")
        r, scale = int(round(rate)), None
    else:
        if cal["scale"] is None:
            raise InputError(f"presets need a calibration scale: {cal['status']}")
        r, scale = rate_for(density, cal["scale"]), cal["scale"]
    meta = {
        "engine": ENGINE,
        "calibration_scale": cal["scale"],
        "calibration_status": cal["status"],
        "density": density,
        "rate_per_entry_veh_h": r,
        "rate_basis": "calibrated preset (browser rate x scale)" if scale else "custom raw SUMO rate, not calibrated",
        "oversaturated": True if density == "high" else (None if density == "custom" else False),
        "traffic_note": ("High traffic is oversaturated by design: queues keep growing, so waits and queues are large for every strategy."
                         if density == "high" else
                         "Custom rates are not calibrated; check the queues before trusting the comparison." if density == "custom"
                         else "Calibrated so that Strategy A does not oversaturate at this level."),
        "origin": origin, "dest": dest, "seed": seed, "blocked": list(blocked),
    }
    if ambulances:
        meta["ambulances"] = [{"id": f"amb{i}", "origin": a["origin"], "dest": a["dest"], "start": a.get("start")}
                              for i, a in enumerate(ambulances)]
    meta["origin"], meta["dest"] = origin, dest
    return Scenario(origin=origin, dest=dest, density="custom", rate=float(r), seed=seed, blocked=blocked,
                    ambulances=amb_specs), meta


def live_fields(run) -> dict:
    """Extra response fields for a run started with live=true (empty for every other run)."""
    lv = run.live
    if lv is None:
        return {}
    return {"live_enabled": True, "modified_live": lv.modified, "clean_comparison": not lv.modified,
            "live_events": list(lv.events), "ambulance_count": lv.amb_total}


def clean(x):
    """JSON-safe copy: non-finite floats become None."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    return x


def summarize(result) -> dict:
    return clean({"strategy": result.strategy, "metrics": result.metrics, "safety": result.safety,
                  "switches": result.switches, "wall_seconds": result.wall_seconds, "routes_sha1": result.routes_sha1})


def deltas_vs_a(results: dict) -> dict:
    """Percent change versus A from the run records (negative = lower than A; whether lower is better depends on the metric)."""
    base = results["A"]["metrics"]
    out = {}
    for s, r in results.items():
        if s == "A":
            continue
        d = {}
        for k in COMPARE_KEYS:
            a, v = base.get(k), r["metrics"].get(k)
            ok = isinstance(a, (int, float)) and isinstance(v, (int, float)) and a
            d[k] = round((v - a) / a * 100, 2) if ok else None
        out[s] = d
    return out


def default_runner(s, strategy, label, cfg, on_step, live=None):
    from pegc_sumo.runner import run_scenario     # imported late: the API starts without SUMO
    if live is None:
        return run_scenario(s, strategy, label=label, cfg=cfg, on_step=on_step)
    return run_scenario(s, strategy, label=label, cfg=cfg, on_step=on_step, live=live)


@dataclass
class Run:
    id: str
    kind: str                      # "run" | "compare"
    strategies: tuple
    scenario: Scenario
    meta: dict
    status: str = "queued"         # queued | running | done | error
    summary: dict | None = None
    results: dict | None = None    # compare only
    error: str | None = None
    snapshots: list = field(default_factory=list)
    log: list | None = None        # full decision log of a single run (sent with the WebSocket summary)
    done: threading.Event = field(default_factory=threading.Event)
    created: float = field(default_factory=time.time)
    live: LiveControl | None = None   # Step 10: only for runs started with live=true


class RunManager:
    """One worker thread, one run at a time (scenarios share one routes file). Strategies of a compare run one after another."""

    def __init__(self, runner=None, snapshot_interval: float = 0.125, max_queued: int = 20, max_kept: int = 200,
                 max_snapshots: int = 3000, sim_interval: float | None = None, live_speed: float | None = 12.0):
        self.live_speed = live_speed              # Step 10: simulated seconds per wall second for live runs
        self.sim_interval = sim_interval          # None: sample by wall clock (old behaviour); else every N simulated seconds
        self.runner = runner or default_runner
        self.interval, self.max_queued, self.max_kept, self.max_snapshots = snapshot_interval, max_queued, max_kept, max_snapshots
        self.runs: dict = {}
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.current: str | None = None
        self.c3 = load_c3()
        threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, kind: str, strategies: tuple, scenario: Scenario, meta: dict, live: bool = False) -> Run:
        with self.lock:
            if sum(1 for r in self.runs.values() if r.status in ("queued", "running")) >= self.max_queued:
                raise Busy("too many runs waiting; try again later")
            run = Run(uuid.uuid4().hex[:12], kind, strategies, scenario, dict(meta))
            if "C3" in strategies:
                run.meta["c3_weight"] = self.c3["weight"]
                run.meta["c3_weight_source"] = self.c3["source"]
            if live and kind == "run":
                run.live = LiveControl(len(scenario.ambulances) or 1, self.live_speed)
            self.runs[run.id] = run
            for old in [r.id for r in sorted(self.runs.values(), key=lambda r: r.created)
                        if r.status in ("done", "error", "cancelled")][:max(0, len(self.runs) - self.max_kept)]:
                del self.runs[old]
        self.q.put(run.id)
        return run

    def get(self, run_id: str):
        return self.runs.get(run_id)

    def cancel(self, run_id: str):
        """Cancel a waiting run, or stop a running live run. Returns the run (None if unknown). A running run that is not
        live (Compare, Stress) cannot be interrupted and simply continues."""
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                return None
            if run.status == "queued":
                run.status = "cancelled"
                run.done.set()
            elif run.status == "running" and run.live is not None:
                run.live.cancel()
            return run

    def busy(self) -> dict:
        with self.lock:
            return {"running": self.current,
                    "waiting": sum(1 for r in self.runs.values() if r.status == "queued")}

    def _sampler(self, run: Run):
        """on_step callback. Wall-clock sampling; log entries of skipped steps are carried into the next kept snapshot.
        `frame_wanted` tells a runner that supports per-vehicle frames when to build one (Step 9a)."""
        st = {"last": None, "armed": False, "frames": False, "pending": [], "steps": 0, "last_step": None}

        def due() -> bool:
            if self.sim_interval is not None:             # simulated-time sampling: smooth playback however fast SUMO runs
                return st["last_step"] is None or (st["steps"] + 1 - st["last_step"]) * CONFIG.dt >= self.sim_interval - 1e-9
            return st["last"] is None or time.monotonic() - st["last"] >= self.interval

        def frame_wanted() -> bool:
            st["frames"] = True
            st["armed"] = due()
            return st["armed"]

        def on_step(snap):
            st["steps"] += 1
            st["pending"].extend(snap.get("log") or [])
            keep = st["armed"] if st["frames"] else due()
            st["armed"] = False
            if not keep:
                return
            st["last"] = time.monotonic()
            st["last_step"] = st["steps"]
            if len(run.snapshots) < self.max_snapshots:
                out = {k: v for k, v in snap.items() if k != "log"}
                out["log"], st["pending"] = st["pending"], []
                run.snapshots.append({"engine": ENGINE, "run_id": run.id, "strategy": run.strategies[0], **clean(out)})
        on_step.frame_wanted = frame_wanted
        return on_step

    def _worker(self):
        while True:
            run = self.runs.get(self.q.get())
            if run is None:
                continue
            with self.lock:
                if run.status == "cancelled":            # cancelled while waiting in the queue
                    continue
                run.status, self.current = "running", run.id
            try:
                results = {}
                for st in run.strategies:
                    cfg = replace(CONFIG, c3_wait_weight=self.c3["weight"]) if st == "C3" else CONFIG
                    args = (run.scenario, st, f"api_{run.id}_{st}", cfg, self._sampler(run) if run.kind == "run" else None)
                    res = self.runner(*args, live=run.live) if run.live is not None else self.runner(*args)
                    results[st] = summarize(res)
                    if run.kind == "run":
                        run.log = clean(getattr(res, "log", None))
                if run.kind == "run":
                    run.summary = results[run.strategies[0]]
                    if run.live is not None:
                        run.summary.update(modified_live=run.live.modified, clean_comparison=not run.live.modified,
                                           live_events=clean(list(run.live.events)))
                else:
                    run.results = results
                run.status = "cancelled" if (run.live is not None and run.live.cancelled) else "done"
            except Exception as e:                       # noqa: BLE001 - report any failure to the client
                run.error, run.status = f"{type(e).__name__}: {e}", "error"
            finally:
                if run.live is not None:
                    run.live.close()
                with self.lock:
                    self.current = None
                run.done.set()

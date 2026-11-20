"""Metrics computed ONLY from what happened in the run (same definitions as the browser engine).

Per step the recorder receives each vehicle's speed and road. From these it derives waiting,
queues, ambulance stops, throughput and the recovery time. Nothing is typed in.
"""
from __future__ import annotations

from .config import CONFIG, Config
from .network import Network, sumo_edge_id


def percentile(sorted_values: list, q: float):
    """Nearest-rank percentile (q in 0..100) of an ascending list; None if empty."""
    if not sorted_values:
        return None
    k = max(1, -(-len(sorted_values) * q // 100))      # ceil(n*q/100)
    return sorted_values[int(k) - 1]


class AmbRecord:
    """Step 10: per-ambulance bookkeeping (only used when a run has more than one ambulance)."""

    def __init__(self, aid: str, last_j_edge, free_time: float, origin: str = "", dest: str = ""):
        self.id, self.last_j_edge, self.free_time = aid, last_j_edge, free_time
        self.origin, self.dest = origin, dest
        self.enter = self.exit = self.t_last = None
        self.stops, self.stopped, self.on_last = 0, False, False


class MetricsRecorder:
    def __init__(self, net: Network, cfg: Config = CONFIG, amb_id: str = "amb0", amb_route_edges: list | None = None):
        self.cfg = cfg
        self.amb_id = amb_id
        # edges that end at a junction (where queues form)
        self.queue_edges = {sumo_edge_id(e.id) for e in net.edges if net.nodes[e.to].type == "junction"}
        # the ambulance's last edge that ends at a junction: leaving it means "last junction cleared"
        self.last_j_edge = None
        if amb_route_edges:
            for eid in reversed(amb_route_edges):
                se = sumo_edge_id(eid)
                if se in self.queue_edges:
                    self.last_j_edge = se
                    break
        self.wait = {}          # vehicle -> total waiting time
        self.streak = {}        # vehicle -> current waiting streak
        self.wait_done = 0.0
        self.done_waits = []    # total wait of every finished ordinary vehicle (for the 95th percentile)
        self.done_count = 0
        self.completed = 0
        self.entered = 0
        self.max_streak = 0.0
        self.max_queue = 0
        self.total_queued = 0
        self.series = []
        self.step_index = 0
        self.baseline_sum = 0.0
        self.baseline_n = 0
        self.baseline = None
        self.recovery_time = None
        self.recovery_window = None
        self.t_last = None       # time the ambulance cleared its last junction
        self.amb_enter = None
        self.amb_exit = None
        self.amb_stops = 0
        self.amb_stopped = False
        self.amb_on_last = False
        self.passes = []
        self.log = []
        self.t = 0.0
        self.live = set()        # ordinary vehicles currently in the network
        self.amb_stats: dict = {}   # Step 10: ambulance id -> AmbRecord (empty = single-ambulance run, old behaviour)

    def register_amb(self, aid: str, route_edges: list, free_time: float, origin: str = "", dest: str = "") -> None:
        """Track one more ambulance (Step 10). Call for every ambulance of a run that may have several."""
        last = None
        for eid in reversed(route_edges or []):
            se = sumo_edge_id(eid)
            if se in self.queue_edges:
                last = se
                break
        self.amb_stats[aid] = AmbRecord(aid, last, free_time, origin, dest)

    def _amb_depart(self, vid: str, t_enter: float) -> None:
        r = self.amb_stats.get(vid)
        if r is not None:
            r.enter = t_enter
            if vid != self.amb_id:
                self.event(t_enter, "enter", f"Ambulance {vid} entered the network at t = {t_enter:.1f} s.", time=t_enter, amb=vid)

    def _amb_move(self, vid: str, t_end: float, speed: float, road: str) -> None:
        r = self.amb_stats.get(vid)
        if r is None:
            return
        cfg = self.cfg
        if speed < cfg.amb_stop_speed and not r.stopped:
            r.stopped = True
            r.stops += 1
        elif speed > cfg.amb_resume_speed:
            r.stopped = False
        if r.last_j_edge is not None:
            if road == r.last_j_edge:
                r.on_last = True
            elif r.on_last and r.t_last is None:
                r.t_last = t_end
                if vid != self.amb_id:
                    self.event(t_end, "pass", f"Ambulance {vid} cleared its last junction at t = {t_end:.1f} s.", time=t_end, amb=vid)

    def _amb_arrive(self, vid: str, t_end: float) -> None:
        r = self.amb_stats.get(vid)
        if r is not None:
            r.exit = t_end
            if vid != self.amb_id:
                self.event(t_end, "arrive", f"Ambulance {vid} reached its destination after {t_end - (r.enter or 0):.1f} s "
                           f"({r.stops} stop{'s' if r.stops != 1 else ''}).", time=t_end - (r.enter or 0), amb=vid)

    def event(self, t: float, kind: str, text: str, **data) -> None:
        self.log.append({"t": round(t, 3), "kind": kind, "text": text, "data": data})

    def on_step(self, t_end: float, departed: list, arrived: list, vehicles: dict, safety_violations: int = 0) -> None:
        """vehicles: id -> (speed, road_id) for every vehicle currently in the network."""
        cfg = self.cfg
        self.t = t_end
        self.step_index += 1
        for vid in departed:
            if self.amb_stats and vid.startswith("amb"):
                self._amb_depart(vid, t_end - cfg.dt)
            if vid == self.amb_id:
                self.amb_enter = t_end - cfg.dt      # inserted during the step that just ended
                self.event(self.amb_enter, "enter", f"Ambulance entered the network at t = {self.amb_enter:.1f} s.", time=self.amb_enter)
            elif not vid.startswith("amb"):
                self.entered += 1
        # waiting, queues
        self.live = {v for v in vehicles if not v.startswith("amb")}
        total = 0
        per_edge = {}
        for vid, (speed, road) in vehicles.items():
            if vid.startswith("amb"):
                if self.amb_stats:
                    self._amb_move(vid, t_end, speed, road)
                if vid == self.amb_id:
                    self._amb_step(t_end, speed, road)
                continue
            if speed < cfg.wait_speed:
                self.wait[vid] = self.wait.get(vid, 0.0) + cfg.dt
                self.streak[vid] = self.streak.get(vid, 0.0) + cfg.dt
                if self.streak[vid] > self.max_streak:
                    self.max_streak = self.streak[vid]
            else:
                self.streak[vid] = 0.0
            if road in self.queue_edges and speed < cfg.queue_speed:
                per_edge[road] = per_edge.get(road, 0) + 1
                total += 1
        if per_edge:
            self.max_queue = max(self.max_queue, max(per_edge.values()))
        self.total_queued = total
        if self.step_index % 4 == 0:
            self.series.append([round(t_end, 3), total])
        for vid in arrived:
            if self.amb_stats and vid.startswith("amb"):
                self._amb_arrive(vid, t_end)
            if vid == self.amb_id:
                self.amb_exit = t_end
                self.event(t_end, "arrive", f"Ambulance reached its destination after {t_end - (self.amb_enter or 0):.1f} s ({self.amb_stops} stop{'s' if self.amb_stops != 1 else ''}).",
                           time=t_end - (self.amb_enter or 0))
            elif not vid.startswith("amb"):
                self.completed += 1
                w = self.wait.pop(vid, 0.0)
                self.wait_done += w
                self.done_waits.append(w)
                self.streak.pop(vid, None)
                self.done_count += 1
        # baseline: average queue in the window before the ambulance enters
        if cfg.warmup - cfg.baseline_window + 1e-9 < t_end <= cfg.warmup + 1e-9:
            self.baseline_sum += total
            self.baseline_n += 1
        if self.baseline is None and t_end >= cfg.warmup - 1e-9 and self.baseline_n > 0:
            self.baseline = self.baseline_sum / self.baseline_n
        # recovery rule (identical for every strategy)
        if self.t_last is not None and self.recovery_time is None and self.baseline is not None:
            if total <= self.baseline + cfg.recovery_tolerance:
                if self.recovery_window is None:
                    self.recovery_window = t_end
                if t_end - self.recovery_window >= cfg.recovery_sustain - 1e-9:
                    self.recovery_time = max(0.0, self.recovery_window - self.t_last)
            else:
                self.recovery_window = None

    def _amb_step(self, t_end: float, speed: float, road: str) -> None:
        cfg = self.cfg
        if speed < cfg.amb_stop_speed and not self.amb_stopped:
            self.amb_stopped = True
            self.amb_stops += 1
        elif speed > cfg.amb_resume_speed:
            self.amb_stopped = False
        if self.last_j_edge is not None:
            if road == self.last_j_edge:
                self.amb_on_last = True
            elif self.amb_on_last and self.t_last is None:
                self.t_last = t_end
                self.event(t_end, "pass", f"Ambulance cleared its last junction at t = {t_end:.1f} s.", time=t_end)

    def metrics(self) -> dict:
        ws, n = self.wait_done, self.done_count
        for vid in self.live:
            ws += self.wait.get(vid, 0.0)
            n += 1
        t_now = max(self.t, 1e-9)
        amb_time = None if self.amb_exit is None or self.amb_enter is None else self.amb_exit - self.amb_enter
        waits = sorted([self.wait.get(v, 0.0) for v in self.live] + self.done_waits)
        out = {
            "waitP95": percentile(waits, 95.0),
            "ambTravelTime": amb_time,
            "ambFinished": self.amb_exit is not None,
            "ambStops": self.amb_stops,
            "avgWait": ws / n if n else 0.0,
            "longestWait": self.max_streak,
            "maxQueue": self.max_queue,
            "throughput": self.completed * 3600 / t_now,
            "completed": self.completed,
            "entered": self.entered,
            "recoveryTime": self.recovery_time,
            "recoveryBaseline": self.baseline,
            "totalQueued": self.total_queued,
            "time": self.t,
        }
        if len(self.amb_stats) > 1:                      # Step 10: added keys, only for runs with several ambulances
            out.update(self._amb_metrics())
        return out

    def _amb_metrics(self) -> dict:
        rows = []
        for aid, r in self.amb_stats.items():
            done = r.exit is not None and r.enter is not None
            tt = r.exit - r.enter if done else None
            rows.append({"id": aid, "origin": r.origin, "dest": r.dest, "startTime": r.enter, "finished": done,
                         "travelTime": tt, "freeFlowTime": r.free_time, "delay": None if tt is None else tt - r.free_time,
                         "stops": r.stops})
        fin = [x for x in rows if x["finished"]]
        return {"ambulances": rows,
                "ambCount": len(rows),
                "ambFinishedCount": len(fin),
                "ambAllFinished": len(fin) == len(rows),
                "ambMeanDelay": sum(x["delay"] for x in fin) / len(fin) if fin else None,
                "ambWorstDelay": max(x["delay"] for x in fin) if fin else None,
                "ambMeanTravelTime": sum(x["travelTime"] for x in fin) / len(fin) if fin else None,
                "ambWorstTravelTime": max(x["travelTime"] for x in fin) if fin else None}

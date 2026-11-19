"""SUMO vehicle positions -> the browser renderer's format (browser edge id + distance along it). Pure Python, no SUMO import.

Rules (Step 9a):
  * SUMO edge "W0_to_J00" is browser edge "W0>J00".
  * Lane length is scaled to the browser edge length (205.6 -> 220, 172.8 -> 180): s = pos * browser_len / sumo_len.
  * A vehicle on an internal junction lane (":J00_3_0") is placed on the NEXT edge of its route at s = 0
    ("prev" names the edge it came from, so the renderer can draw the turn).
  * s is the front-bumper position, like the browser engine's `v.s`.
"""
from __future__ import annotations

import zlib
from typing import Callable

from .config import CONFIG, Config
from .network import Network

N_COLORS = 6          # the browser engine draws cars with color = int(6)
BRAKE_DECEL = 0.5     # m/s^2: braking light on when the speed fell faster than this since the previous step


def browser_edge_id(road: str) -> str | None:
    """"W0_to_J00" -> "W0>J00"; internal lanes and unknown ids -> None."""
    if not road or road.startswith(":") or "_to_" not in road:
        return None
    return road.replace("_to_", ">")


def internal_junction(road: str) -> str | None:
    """":J00_3_0" or ":J00_3" -> "J00"."""
    return road[1:].split("_")[0] if road.startswith(":") else None


def car_color(vid: str) -> int:
    return zlib.crc32(vid.encode()) % N_COLORS


class FrameBuilder:
    def __init__(self, net: Network, cfg: Config, lane_len: Callable[[str], float], route_of: Callable[[str], list]):
        """lane_len(sumo_edge) -> real lane length; route_of(vehicle id) -> SUMO edge ids of its route."""
        self.net, self.cfg, self.lane_len, self.route_of = net, cfg, lane_len, route_of
        self._routes: dict = {}

    def _route(self, vid: str) -> list:
        r = self._routes.get(vid)
        if r is None:
            r = self._routes[vid] = [browser_edge_id(e) or e for e in self.route_of(vid)]
        return r

    def pose(self, vid: str, road: str, pos: float):
        """-> (browser edge id, s, prev edge id or None) or None when the vehicle cannot be placed."""
        bid = browser_edge_id(road)
        if bid is not None:
            e = self.net.edge_by_id.get(bid)
            if e is None:
                return None
            L = self.lane_len(road)
            s = pos * e.length / L if L > 0 else 0.0
            return bid, min(max(s, 0.0), e.length), None
        j = internal_junction(road)
        if j is None:
            return None
        nxt = prv = None
        for b in self._route(vid):
            e = self.net.edge_by_id.get(b)
            if e is None:
                continue
            if e.frm == j and nxt is None:
                nxt = b
            if e.to == j:
                prv = b
        return None if nxt is None else (nxt, 0.0, prv)

    def vehicle(self, vid: str, road: str, pos: float, speed: float, prev_speed: float | None = None) -> dict | None:
        p = self.pose(vid, road, pos)
        if p is None:
            return None
        amb = vid.startswith("amb")
        braking = prev_speed is not None and (prev_speed - speed) / self.cfg.dt > BRAKE_DECEL
        return {"id": vid, "kind": "amb" if amb else "car", "edge": p[0], "s": round(p[1], 2), "v": round(speed, 2),
                "color": 0 if amb else car_color(vid), "len": self.cfg.amb_length if amb else self.cfg.car_length,
                "braking": bool(braking), "prev": p[2]}

    def frame(self, items, prev_speeds: dict | None = None) -> list:
        """items: iterable of (vid, road, lane position, speed). Vehicles that cannot be placed are left out."""
        out = []
        for vid, road, pos, speed in items:
            v = self.vehicle(vid, road, pos, speed, (prev_speeds or {}).get(vid))
            if v is not None:
                out.append(v)
        return out

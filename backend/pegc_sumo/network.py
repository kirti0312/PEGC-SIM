"""Road graph identical to src/engine/network.js, plus SUMO file generation.

Browser coordinates have y pointing down; SUMO has y pointing up. SUMO files use
y_sumo = -y_browser, so "row 0" stays at the top of the map in both worlds.
Edge ids: browser "W0>J00"  <->  SUMO "W0_to_J00".
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

from .config import CONFIG, Config


@dataclass
class Node:
    id: str
    x: float
    y: float
    type: str  # "junction" | "boundary"


@dataclass
class Edge:
    id: str
    frm: str
    to: str
    length: float
    seg_id: str
    is_entry: bool


@dataclass
class Segment:
    id: str
    a: str
    b: str


class Network:
    def __init__(self, cfg: Config = CONFIG):
        self.cfg = cfg
        R, C, B, L = cfg.rows, cfg.cols, cfg.block_length, cfg.boundary_length
        self.node_list: list[Node] = []
        for r in range(R):
            for c in range(C):
                self.node_list.append(Node(f"J{r}{c}", c * B, r * B, "junction"))
        for r in range(R):
            self.node_list.append(Node(f"W{r}", -L, r * B, "boundary"))
            self.node_list.append(Node(f"E{r}", (C - 1) * B + L, r * B, "boundary"))
        for c in range(C):
            self.node_list.append(Node(f"N{c}", c * B, -L, "boundary"))
            self.node_list.append(Node(f"S{c}", c * B, (R - 1) * B + L, "boundary"))
        self.nodes = {n.id: n for n in self.node_list}

        self.segments: list[Segment] = []
        pairs = []
        for r in range(R):
            pairs.append((f"W{r}", f"J{r}0"))
            for c in range(C - 1):
                pairs.append((f"J{r}{c}", f"J{r}{c + 1}"))
            pairs.append((f"J{r}{C - 1}", f"E{r}"))
        for c in range(C):
            pairs.append((f"N{c}", f"J0{c}"))
            for r in range(R - 1):
                pairs.append((f"J{r}{c}", f"J{r + 1}{c}"))
            pairs.append((f"J{R - 1}{c}", f"S{c}"))

        self.edges: list[Edge] = []
        self.out_edges: dict[str, list[Edge]] = {n.id: [] for n in self.node_list}
        for a, b in pairs:
            seg = Segment(f"{a}|{b}", a, b)
            self.segments.append(seg)
            for frm, to in ((a, b), (b, a)):
                na, nb = self.nodes[frm], self.nodes[to]
                length = math.hypot(nb.x - na.x, nb.y - na.y)
                e = Edge(f"{frm}>{to}", frm, to, length, seg.id, na.type == "boundary")
                self.edges.append(e)
                self.out_edges[frm].append(e)
        self.edge_by_id = {e.id: e for e in self.edges}
        self.boundary_ids = [n.id for n in self.node_list if n.type == "boundary"]
        self.entry_edges = [e for e in self.edges if e.is_entry]
        self.junction_ids = [n.id for n in self.node_list if n.type == "junction"]


def build_network(cfg: Config = CONFIG) -> Network:
    return Network(cfg)


def find_route(net: Network, from_id: str, to_id: str, blocked=frozenset()):
    """Faithful port of findRoute(): same scan order, same tie-breaking."""
    if from_id == to_id or from_id not in net.nodes or to_id not in net.nodes:
        return None
    inf = float("inf")
    dist = {n.id: inf for n in net.node_list}
    prev: dict[str, Edge] = {}
    done: set[str] = set()
    dist[from_id] = 0.0
    while True:
        best, bd = None, inf
        for n in net.node_list:
            if n.id not in done and dist[n.id] < bd:
                bd, best = dist[n.id], n.id
        if best is None or best == to_id:
            break
        done.add(best)
        for e in net.out_edges[best]:
            if e.seg_id in blocked:
                continue
            nd = bd + e.length
            if nd < dist[e.to]:
                dist[e.to] = nd
                prev[e.to] = e
    if dist[to_id] == inf:
        return None
    route, cur = [], to_id
    while cur != from_id:
        e = prev[cur]
        route.insert(0, e)
        cur = e.frm
    return route


def sumo_edge_id(edge_id: str) -> str:
    return edge_id.replace(">", "_to_")


def write_sumo_network(net: Network, out_dir: str, netconvert: str) -> str:
    """Write plain node/edge files and call netconvert. Only the junctions are signalised."""
    import subprocess

    os.makedirs(out_dir, exist_ok=True)
    nod = os.path.join(out_dir, "nodes.nod.xml")
    edg = os.path.join(out_dir, "edges.edg.xml")
    out = os.path.join(out_dir, "net.net.xml")
    with open(nod, "w", encoding="utf-8") as f:
        f.write('<nodes>\n')
        for n in net.node_list:
            t = "traffic_light" if n.type == "junction" else "priority"
            f.write(f'  <node id="{n.id}" x="{n.x:.2f}" y="{-n.y:.2f}" type="{t}"/>\n')
        f.write('</nodes>\n')
    with open(edg, "w", encoding="utf-8") as f:
        f.write('<edges>\n')
        for e in net.edges:
            f.write(f'  <edge id="{sumo_edge_id(e.id)}" from="{e.frm}" to="{e.to}" '
                    f'priority="1" numLanes="1" speed="{net.cfg.car_max_speed}"/>\n')
        f.write('</edges>\n')
    cmd = [netconvert, "--node-files", nod, "--edge-files", edg, "-o", out,
           "--no-turnarounds", "true", "--tls.default-type", "static",
           "--tls.green.time", str(int(net.cfg.fixed_green)),
           "--tls.yellow.time", str(int(net.cfg.yellow)),
           "--tls.allred.time", str(int(net.cfg.all_red)),
           "--no-warnings", "true"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("netconvert failed:\n" + r.stdout + r.stderr)
    return out

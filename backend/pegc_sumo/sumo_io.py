"""Locating SUMO, building the scenario files, and starting a TraCI session."""
from __future__ import annotations

import hashlib
import os
import shutil

from .config import CONFIG, Config, Scenario
from .demand import generate_demand, write_routes
from .network import Network, build_network, write_sumo_network

BUILD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "build")


def find_binary(name: str) -> str:
    """Find a SUMO executable: SUMO_HOME, then PATH, then the pip package location."""
    exe = name + (".exe" if os.name == "nt" else "")
    home = os.environ.get("SUMO_HOME")
    if home:
        p = os.path.join(home, "bin", exe)
        if os.path.exists(p):
            return p
    p = shutil.which(name)
    if p:
        return p
    try:
        import sumo  # the eclipse-sumo wheel
        p = os.path.join(sumo.SUMO_HOME, "bin", exe)
        if os.path.exists(p):
            return p
    except Exception:
        pass
    raise FileNotFoundError(f"Could not find '{name}'. Install with: pip install eclipse-sumo (or set SUMO_HOME).")


def build_static_network(cfg: Config = CONFIG, out_dir: str | None = None) -> tuple[Network, str]:
    """Build (or reuse) the SUMO network. Returns (graph, path to net.net.xml)."""
    net = build_network(cfg)
    out_dir = out_dir or os.path.join(BUILD_DIR, "network")
    path = os.path.join(out_dir, "net.net.xml")
    if not os.path.exists(path):
        write_sumo_network(net, out_dir, find_binary("netconvert"))
    return net, path


def build_scenario_files(net: Network, s: Scenario, cfg: Config = CONFIG, out_dir: str | None = None) -> dict:
    """Write the routes file for one scenario. Returns paths and the generated demand."""
    cars, amb = generate_demand(net, s, cfg)
    key = f"{s.density}_{s.rate}_{s.seed}_{s.origin}_{s.dest}_{'+'.join(sorted(s.blocked)) or 'open'}"
    out_dir = out_dir or os.path.join(BUILD_DIR, "scenarios")
    os.makedirs(out_dir, exist_ok=True)
    routes = os.path.join(out_dir, key + ".rou.xml")
    write_routes(cars, amb, routes, cfg)
    digest = hashlib.sha1(open(routes, "rb").read()).hexdigest()
    return {"routes": routes, "cars": cars, "amb": amb, "sha1": digest}


def start_sumo(net_path: str, routes_path: str, seed: int, label: str, cfg: Config = CONFIG):
    """Start SUMO headless and return a TraCI connection. Caller must call conn.close()."""
    import traci

    cmd = [find_binary("sumo"), "-n", net_path, "-r", routes_path,
           "--step-length", str(cfg.dt), "--seed", str(seed),
           "--no-step-log", "true", "--no-warnings", "true",
           "--time-to-teleport", "-1", "--collision.action", "warn"]
    traci.start(cmd, label=label)
    return traci.getConnection(label)

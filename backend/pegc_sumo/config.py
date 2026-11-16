"""Parameters mirrored from src/engine/config.js. Keep the two files in sync.

If a value changes in the browser engine, change it here too, then re-run
`node scripts/export_demand_golden.mjs` and `pytest` so drift is caught.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    # time
    dt: float = 0.25
    warmup: float = 60.0
    max_sim_time: float = 420.0
    # geometry (metres)
    rows: int = 2
    cols: int = 3
    block_length: float = 220.0
    boundary_length: float = 180.0
    # vehicles
    car_length: float = 4.5
    amb_length: float = 6.5
    min_gap: float = 2.0
    car_max_speed: float = 13.9
    amb_max_speed: float = 19.4
    accel: float = 2.0
    amb_accel: float = 3.0
    decel: float = 3.5
    hard_decel: float = 7.0
    time_headway: float = 1.1
    speed_jitter: float = 0.12
    # traffic (vehicles per hour per entry point)
    density: dict = field(default_factory=lambda: {"low": 180, "medium": 330, "high": 520})
    rate_min: int = 100
    rate_max: int = 900
    # signals (used from step 2)
    min_green: float = 7.0
    yellow: float = 3.0
    all_red: float = 2.0
    max_emergency_hold: float = 25.0
    max_wait: float = 90.0
    lockout: float = 4.0
    fixed_green: float = 20.0


CONFIG = Config()


@dataclass
class Scenario:
    origin: str = "W0"
    dest: str = "E1"
    density: str = "medium"        # low | medium | high | custom
    rate: float | None = None      # veh/h per entry, used when density == "custom"
    seed: int = 7
    blocked: tuple = ()            # segment ids such as "J00|J01"
    strategy: str = "A"


def entry_rate(s: Scenario, cfg: Config = CONFIG) -> int:
    """Same rule as entryRate() in config.js."""
    if s.density == "custom":
        try:
            r = float(s.rate)
        except (TypeError, ValueError):
            return cfg.density["medium"]
        if r != r or r in (float("inf"), float("-inf")):
            return cfg.density["medium"]
        return int(min(cfg.rate_max, max(cfg.rate_min, round(r))))
    return cfg.density.get(s.density, cfg.density["medium"])

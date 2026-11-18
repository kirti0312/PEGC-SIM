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
    # metrics (same definitions as the browser engine)
    wait_speed: float = 0.5          # m/s below which a vehicle counts as waiting
    queue_speed: float = 1.0         # m/s below which a vehicle on a junction approach counts as queued
    amb_stop_speed: float = 0.3      # ambulance counts a stop below this speed ...
    amb_resume_speed: float = 1.5    # ... and can stop again only after exceeding this
    recovery_tolerance: float = 2.0  # queued vehicles above the pre-ambulance average
    recovery_sustain: float = 10.0   # seconds the queue must stay within tolerance
    baseline_window: float = 30.0    # seconds before ambulance entry used for the baseline
    # signals
    min_green: float = 7.0
    yellow: float = 3.0
    all_red: float = 2.0
    max_emergency_hold: float = 25.0
    max_wait: float = 90.0
    lockout: float = 4.0
    fixed_green: float = 20.0
    # strategy B (mirrors bTriggerDistance in config.js)
    b_trigger_distance: float = 120.0   # m from the stop line
    # rule-based ETA (mirrors cEtaCruise in config.js; also used by Strategy C from Step 4)
    c_eta_cruise: float = 14.5          # m/s assumed cruise speed in the rule-based ETA
    # strategy C / PEGC (mirror config.js; added in Step 4)
    c_lookahead_junctions: int = 2
    c_discharge_headway: float = 2.0    # s per queued vehicle discharge
    c_min_delay_saved: float = 1.0      # s; below this PEGC does not intervene
    c_weight_amb_delay: float = 12.0    # value of one second of ambulance delay
    c_cost_per_car: float = 1.0         # cost of one second of delay for one cross-street car
    c_wait_weight: float = 0.5          # weight on the longest cross-street wait
    c_spillback_penalty: float = 4.0
    c_recovery_max: float = 60.0        # s: recovery mode timeout per junction


CONFIG = Config()


@dataclass
class AmbulanceSpec:
    id: str
    origin: str
    dest: str
    start_t: float | None = None   # None means "after the warm-up"


@dataclass
class Scenario:
    origin: str = "W0"
    dest: str = "E1"
    density: str = "medium"        # low | medium | high | custom
    rate: float | None = None      # veh/h per entry, used when density == "custom"
    seed: int = 7
    blocked: tuple = ()            # segment ids such as "J00|J01"
    strategy: str = "A"
    # Several ambulances can be described here (used from Step 10). Empty means one ambulance
    # built from origin/dest. Step 2 measures the first ambulance only.
    ambulances: tuple = ()

    def ambulance_specs(self, cfg: "Config" = None) -> list:
        cfg = cfg or CONFIG
        if self.ambulances:
            return [AmbulanceSpec(a.id, a.origin, a.dest, cfg.warmup if a.start_t is None else a.start_t)
                    for a in self.ambulances]
        return [AmbulanceSpec("amb0", self.origin, self.dest, cfg.warmup)]


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

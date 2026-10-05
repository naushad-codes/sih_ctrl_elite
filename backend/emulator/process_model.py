"""Independent deterministic station emulator; intentionally does not import the forecaster."""
from datetime import datetime, timezone

EMULATOR_VERSION = "edge-emulator-v1.0"
PROFILES = {
    "maitri": {"power_kw": 180.0, "critical_kw": 112.0, "deferrable_kw": 24.0, "fuel_l": 42000.0, "indoor_c": -8.0, "water_production_lph": 85.0, "water_l": 6200.0},
    "bharati": {"power_kw": 210.0, "critical_kw": 128.0, "deferrable_kw": 31.0, "fuel_l": 51000.0, "indoor_c": -7.0, "water_production_lph": 92.0, "water_l": 7100.0},
}


def state_at(station_id: str, tick: int, seed: int = 26060, fault_at: int | None = None, perturbation: float = 0.0) -> dict:
    """Return deterministic synthetic readings for a logical hourly tick."""
    if station_id not in PROFILES:
        raise ValueError("Unknown station profile")
    if tick < 0:
        raise ValueError("tick must be non-negative")
    profile = PROFILES[station_id]
    # Integer arithmetic avoids platform-specific PRNG streams in the demo.
    jitter = (((seed + tick * 17) * 1103515245 + 12345) >> 16) % 7 - 3
    faulted = fault_at is not None and tick >= fault_at
    power = 0.0 if faulted else max(0.0, profile["power_kw"] + jitter * 0.2 + perturbation)
    fuel = max(0.0, profile["fuel_l"] - tick * (9.5 if station_id == "maitri" else 11.0))
    indoor = profile["indoor_c"] - (1.5 if faulted else 0.0) + jitter * 0.03
    water_rate = 0.0 if faulted else profile["water_production_lph"]
    water = max(0.0, profile["water_l"] + tick * (water_rate * 24.0 - 1000.0))
    observed_at = datetime.fromtimestamp(1760000000 + tick * 3600, timezone.utc).isoformat()
    source = {"source_type": "DEMO_SYNTHETIC", "source": "Independent station emulator", "quality": "SYNTHETIC", "emulator_version": EMULATOR_VERSION, "seed": seed, "tick": tick, "observed_at": observed_at}
    return {
        "station_id": station_id,
        "state": {
            "power_available": {"value": round(power, 2), "unit": "kW", **source},
            "critical_load": {"value": profile["critical_kw"], "unit": "kW", **source},
            "deferrable_load": {"value": profile["deferrable_kw"], "unit": "kW", **source},
            "fuel_reserve": {"value": round(fuel, 2), "unit": "L", **source},
            "heat_indoor": {"value": round(indoor, 2), "unit": "°C", **source},
            "water_production": {"value": water_rate, "unit": "L/h", **source},
            "water_reserve": {"value": round(water, 2), "unit": "L", **source},
            "connectivity": {"value": "CONNECTED", "unit": "state", **source},
            "chp_availability": {"value": "UNAVAILABLE" if faulted else "AVAILABLE", "unit": "state", **source},
        },
        "fault_injected": faulted,
    }

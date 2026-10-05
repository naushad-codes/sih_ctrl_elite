"""Independent fixed-fixture station emulator; never imports the scenario model."""
from copy import deepcopy
from datetime import datetime, timezone

EMULATOR_VERSION = "emulator-v0.1"
FIXTURES = {
    "maitri": {"power_available": (180.0, "kW"), "critical_load": (112.0, "kW"), "deferrable_load": (24.0, "kW"), "fuel_reserve": (42000.0, "L"), "heat_indoor": (-8.0, "°C"), "heat_outdoor": (-24.0, "°C"), "water_production": (85.0, "L/h"), "water_reserve": (6200.0, "L"), "connectivity": ("CONNECTED", "state")},
    "bharati": {"power_available": (210.0, "kW"), "critical_load": (128.0, "kW"), "deferrable_load": (31.0, "kW"), "fuel_reserve": (51000.0, "L"), "heat_indoor": (-7.0, "°C"), "heat_outdoor": (-18.0, "°C"), "water_production": (92.0, "L/h"), "water_reserve": (7100.0, "L"), "connectivity": ("CONNECTED", "state")},
}

def snapshot(station: str, revision: int = 0, timestamp: str | None = None) -> dict:
    """Return a clearly synthetic, deterministic baseline snapshot for an offline demo."""
    fixture = FIXTURES.get(station)
    if fixture is None:
        raise ValueError("Unknown station fixture")
    when = timestamp or datetime.now(timezone.utc).isoformat()
    return {key: {"value": value, "unit": unit, "source": "Station emulator fixture", "source_kind": "DEMO_SYNTHETIC", "timestamp": when, "quality": "UNKNOWN", "revision": revision, "model_version": EMULATOR_VERSION, "evidence_refs": []} for key, (value, unit) in deepcopy(fixture).items()}

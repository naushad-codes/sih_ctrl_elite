"""Load only normalized, locally stored historical weather. Never scrapes remote portals."""
from __future__ import annotations
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "data" / "weather"
REQUIRED = {"timestamp", "station", "temperature_c", "pressure_hpa", "relative_humidity_pct", "wind_speed", "wind_direction_deg", "source", "source_type", "quality"}
ALLOWED_TYPES = {"HISTORICAL_WEATHER", "DEMO_SYNTHETIC", "MANUAL_DEMO_ENTRY"}

def load_weather(station: str, root: Path = ROOT, manifest_name: str = "weather_manifest.json") -> list[dict]:
    manifest_path = root / manifest_name
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    configured = manifest.get("files", {}).get(station, [])
    rows = []
    for relative in configured:
        path = (root / relative).resolve()
        if root.resolve() not in path.parents:
            raise ValueError("Weather file must remain under the local weather data directory")
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8") as stream:
            for raw in csv.DictReader(stream):
                missing = REQUIRED - raw.keys()
                if missing:
                    raise ValueError(f"Weather row missing fields: {', '.join(sorted(missing))}")
                if raw["station"].lower() != station.lower():
                    continue
                if raw["source_type"] not in ALLOWED_TYPES:
                    raise ValueError("Unsupported weather source_type")
                metadata = next((item for item in manifest.get("sources", []) if item.get("station", "").lower() == station.lower() and item.get("source", "") == raw["source"]), {})
                rows.append({**raw, "temperature_c": _float(raw["temperature_c"]), "pressure_hpa": _float(raw["pressure_hpa"]), "relative_humidity_pct": _float(raw["relative_humidity_pct"]), "wind_speed": _float(raw["wind_speed"]), "wind_direction_deg": _float(raw["wind_direction_deg"]), "source_url": metadata.get("source_url"), "dataset": metadata.get("dataset"), "period": metadata.get("period")})
    return sorted(rows, key=lambda row: row["timestamp"])

def _float(value: str):
    try:
        return float(value) if value.strip() else None
    except ValueError:
        return None

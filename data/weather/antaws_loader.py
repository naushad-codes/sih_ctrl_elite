"""Optional AntAWS adapter using the same normalized local CSV contract."""
from pathlib import Path
from .weather_loader import load_weather as _load_weather

ROOT = Path(__file__).resolve().parent / "antaws"

def load_antaws(station: str, root: Path = ROOT) -> list[dict]:
    """Load explicitly listed local AntAWS CSV files; this adapter performs no download."""
    return _load_weather(station, root, manifest_name="manifest.json")

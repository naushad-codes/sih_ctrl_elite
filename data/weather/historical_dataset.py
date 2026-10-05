"""Read bundled historical Maitri monthly summaries without changing source files."""
from __future__ import annotations
import csv
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2] / "dataset"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
FILES = {"temperature_c": "maitriavgtemp.csv", "pressure_hpa": "maitriavgap.csv", "relative_humidity_pct": "maitriavgrh.csv"}
SOURCE = "NCPOR Historical (local monthly summaries)"
SOURCE_TYPE = "NCPOR_HISTORICAL"

def _monthly_values(path: Path) -> dict[tuple[int, int], float]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="latin-1")
    body = re.search(r"<body[^>]*>(.*?)</body>", text, re.S | re.I)
    if body:
        text = re.sub(r"<[^>]+>", "\n", body.group(1))
    rows = list(csv.reader(StringIO(text)))
    header_index = next((i for i, row in enumerate(rows) if row and row[0].strip().strip('"').lower() == "year"), None)
    if header_index is None:
        return {}
    values = {}
    for row in rows[header_index + 1:]:
        if not row:
            continue
        try:
            year = int(row[0].strip().strip('"'))
        except ValueError:
            continue
        for month, raw in enumerate(row[1:13], 1):
            try:
                value = float(raw.strip().strip('"'))
            except (ValueError, AttributeError):
                continue
            values[(year, month)] = value
    return values

def load_local_historical(station: str, root: Path = ROOT) -> list[dict]:
    if station.lower() != "maitri":
        return []
    series = {field: _monthly_values(root / filename) for field, filename in FILES.items()}
    periods = sorted(set().union(*(values.keys() for values in series.values())))
    loaded_at = datetime.now(timezone.utc).isoformat()
    result = []
    for year, month in periods:
        result.append({
            "id": f"maitri-monthly-{year}-{month:02d}",
            "station_id": "maitri",
            "observation_time": f"{year:04d}-{month:02d}",
            "resolution": "MONTHLY_MEAN",
            "temperature_c": series["temperature_c"].get((year, month)),
            "pressure_hpa": series["pressure_hpa"].get((year, month)),
            "relative_humidity_pct": series["relative_humidity_pct"].get((year, month)),
            "wind_speed": None,
            "wind_direction_deg": None,
            "source": SOURCE,
            "source_type": SOURCE_TYPE,
            "quality": "UNKNOWN",
            "loaded_at": loaded_at,
            "source_file_set": "dataset/maitriavg*.csv",
        })
    return result

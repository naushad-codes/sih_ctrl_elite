"""Bounded adapter for NCPOR's public server-rendered observation pages.

The portal does not expose a documented JSON endpoint. This adapter requests only
its fixed station pages and parses the embedded chart observations and summary.
"""
from __future__ import annotations
from datetime import datetime, timezone
from html import unescape
import re
from urllib.request import Request, urlopen

BASE_URL = "https://data.ncpor.res.in"
STATIONS = {"maitri": "Maitri", "bharati": "Bharati"}
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
TIMEOUT_SECONDS = 12
SERIES = {
    "Temperature": "temperature_c",
    "Wind Speed": "wind_speed",
    "Air Pressure": "pressure_hpa",
    "Relative Humidity": "relative_humidity_pct",
}
BOUNDS = {
    "temperature_c": (-100.0, 60.0),
    "pressure_hpa": (700.0, 1100.0),
    "relative_humidity_pct": (0.0, 100.0),
    "wind_speed": (0.0, 200.0),
    "wind_direction_deg": (0.0, 360.0),
}

class NCPORProviderError(RuntimeError):
    pass

class NCPOROfficialProvider:
    source = "NCPOR"
    source_type = "NCPOR_OFFICIAL_OBSERVATION"

    def __init__(self, opener=urlopen):
        self._opener = opener

    def _get(self, path: str) -> str:
        request = Request(f"{BASE_URL}{path}", headers={"User-Agent": "PRATIBIMB prototype observation adapter/1.0", "Accept": "text/html"})
        try:
            with self._opener(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise NCPORProviderError("NCPOR page exceeded the response size limit")
                if response.status != 200:
                    raise NCPORProviderError(f"NCPOR returned HTTP {response.status}")
                return raw.decode("utf-8", "replace")
        except NCPORProviderError:
            raise
        except Exception as error:
            raise NCPORProviderError(f"Could not retrieve NCPOR observation page: {error}") from error

    def fetch_station_observation(self, station_id: str) -> dict:
        station = station_id.strip().lower()
        if station not in STATIONS:
            raise ValueError("station_id must be maitri or bharati")
        page = self._get(f"/{station}/live")
        wind_page = self._get(f"/{station}/wind_d" if station == "maitri" else f"/{station}/wd")
        latest: dict[str, tuple[int, float]] = {}
        for name, block in re.findall(r'name\s*:\s*["\']([^"\']+)["\']\s*,.*?dataPoints\s*:\s*\[(.*?)\]', page, re.S):
            field = SERIES.get(name.strip())
            if not field:
                continue
            points = re.findall(r'\{\s*x\s*:\s*(\d+)\s*,\s*y\s*:\s*(-?\d+(?:\.\d+)?)', block)
            if points:
                timestamp_ms, value = points[-1]
                latest[field] = (int(timestamp_ms), float(value))
        if not latest:
            raise NCPORProviderError("NCPOR page did not contain recognized observation series")
        # The live chart uses a common timestamp for temperature, pressure, humidity and speed.
        chart_times = [value[0] for value in latest.values()]
        observation_ms = max(chart_times)
        if any(abs(item - observation_ms) > 15 * 60 * 1000 for item in chart_times):
            raise NCPORProviderError("NCPOR environmental series do not share a sufficiently close observation time")
        direction, direction_time = self._parse_direction(wind_page)
        observation_time = datetime.fromtimestamp(observation_ms / 1000, timezone.utc)
        quality = "GOOD"
        if direction_time and abs((direction_time - observation_time).total_seconds()) > 15 * 60:
            quality = "SUSPECT"
        values = {field: latest[field][1] if field in latest else None for field in SERIES.values()}
        values["wind_direction_deg"] = direction
        if not any(value is not None for value in values.values()):
            raise NCPORProviderError("NCPOR observation did not include usable values")
        for field, value in values.items():
            if value is not None:
                low, high = BOUNDS[field]
                if not low <= value <= high:
                    raise NCPORProviderError(f"NCPOR value for {field} is outside the accepted range")
        return {
            "station_id": station,
            "observation_time": observation_time.isoformat(),
            **values,
            "wind_speed_unit": "m/s",
            "source": self.source,
            "source_type": self.source_type,
            "quality": quality,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source_url": f"{BASE_URL}/{station}/live",
            "wind_direction_source_url": f"{BASE_URL}/{station}/wind_d" if station == "maitri" else f"{BASE_URL}/{station}/wd",
            "wind_direction_observation_time": direction_time.isoformat() if direction_time else None,
        }

    @staticmethod
    def _parse_direction(page: str) -> tuple[float | None, datetime | None]:
        text = re.sub(r"<[^>]+>", " ", page)
        text = re.sub(r"\s+", " ", unescape(text)).replace("\ufffd", "°")
        match = re.search(r"Wind Direction:\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*°", text, re.I)
        if not match:
            # NCPOR may return a compass point after the degree symbol or omit it in its HTML encoding.
            match = re.search(r"Wind Direction:\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*(?:°|deg)", text, re.I)
        if not match:
            return None, None
        value = float(match.group(1))
        time_match = re.search(r"\b(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}\s+\d{1,2}:\d{2}\s+[AP]M)\b", text)
        timestamp = None
        if time_match:
            try:
                from zoneinfo import ZoneInfo
                timestamp = datetime.strptime(time_match.group(1), "%d %b %Y %I:%M %p").replace(tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc)
            except ValueError:
                timestamp = None
        return value, timestamp

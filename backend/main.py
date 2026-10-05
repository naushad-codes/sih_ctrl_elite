from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
import secrets
import sqlite3
import hashlib
import json
import os
from typing import Literal

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from .model.scenario_model import MODEL_VERSION, run_scenario
from .emulator.world import snapshot as emulator_snapshot
from data.weather.weather_loader import load_weather
from .data.providers.ncpor_official import NCPOROfficialProvider, NCPORProviderError
from data.weather.historical_dataset import load_local_historical

DB_PATH = Path(os.environ.get("PRATIBIMB_DB_PATH", Path(__file__).with_name("pratibimb.db")))
SYNC_GATEWAY_TOKEN = os.environ.get("PRATIBIMB_GATEWAY_TOKEN", "local-sync-token")

STATIONS = [
    {"id": "bharati", "name": "Bharati Station", "location": "Larsemann Hills, East Antarctica", "code": "BHARATI"},
    {"id": "maitri", "name": "Maitri Station", "location": "Schirmacher Oasis, East Antarctica", "code": "MAITRI"},
]
ROLES = [
    {"id": "hq", "title": "HQ Operations Analyst", "login": "admin_hq", "summary": "Station overview, incidents, planning and remote operation proposals."},
    {"id": "station", "title": "Station Operations Officer", "login": "admin_station", "summary": "Incoming proposals, acknowledgement, local observations and task outcomes."},
    {"id": "technical", "title": "Technical Engineer", "login": "admin_tech", "summary": "Technical readings, inspection tasks, asset observations and reports."},
]
USERS = {"admin_hq": "hq", "admin_station": "station", "admin_tech": "technical"}
NCPOR_PROVIDER = NCPOROfficialProvider()

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="PRATIBIMB Prototype API", lifespan=lifespan)
DEFAULT_ALLOWED_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
ALLOWED_ORIGINS = [origin.strip() for origin in os.environ.get("PRATIBIMB_CORS_ORIGINS", DEFAULT_ALLOWED_ORIGINS).split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health() -> dict:
    return {"service":"hq-api","database":str(DB_PATH),"status":"READY"}


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def database() -> sqlite3.Connection:
    connection = get_connection()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with database() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, username TEXT NOT NULL, role TEXT NOT NULL, station TEXT NOT NULL, created_at TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS stations (id TEXT PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL, code TEXT NOT NULL, profile_version TEXT NOT NULL DEFAULT 'v1')")
        connection.execute("CREATE TABLE IF NOT EXISTS station_state (station TEXT NOT NULL, field TEXT NOT NULL, value_json TEXT NOT NULL, source_kind TEXT NOT NULL, timestamp TEXT, quality TEXT NOT NULL, revision INTEGER NOT NULL, model_version TEXT, unit TEXT NOT NULL, PRIMARY KEY (station, field))")
        for profile in STATIONS:
            connection.execute("INSERT OR IGNORE INTO stations (id, name, location, code) VALUES (?, ?, ?, ?)", (profile['id'], profile['name'], profile['location'], profile['code']))
            for field, item in emulator_snapshot(profile['id'], 0).items():
                connection.execute("INSERT OR IGNORE INTO station_state (station, field, value_json, source_kind, timestamp, quality, revision, model_version, unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (profile['id'], field, json.dumps(item['value']), item['source_kind'], item['timestamp'], item['quality'], item['revision'], item['model_version'], item['unit']))
        connection.execute("""CREATE TABLE IF NOT EXISTS station_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT NOT NULL, event_type TEXT NOT NULL,
            source TEXT NOT NULL, occurred_at TEXT NOT NULL, payload TEXT NOT NULL,
            revision_hash TEXT NOT NULL, model_version TEXT NOT NULL DEFAULT 'v0.1.0',
            manifest_version TEXT NOT NULL DEFAULT 'v1', evidence_refs TEXT NOT NULL DEFAULT '[]')""")
        connection.execute("""CREATE TABLE IF NOT EXISTS measurement_requests (
            id TEXT PRIMARY KEY, station TEXT NOT NULL, metric TEXT NOT NULL, unit TEXT NOT NULL,
            reason TEXT NOT NULL, deadline TEXT NOT NULL, related_scenario TEXT,
            base_revision INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
            created_by TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS inspection_requests (
            id TEXT PRIMARY KEY, station TEXT NOT NULL, asset TEXT NOT NULL, requested_checks TEXT NOT NULL,
            reason TEXT NOT NULL, deadline TEXT NOT NULL, base_revision INTEGER NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS scenarios (
            id TEXT PRIMARY KEY, station TEXT NOT NULL, name TEXT NOT NULL, base_revision INTEGER NOT NULL,
            model_version TEXT NOT NULL, created_at TEXT NOT NULL, inputs_json TEXT NOT NULL,
            baseline_json TEXT NOT NULL, result_json TEXT NOT NULL, status TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS scenario_results (
            scenario_id TEXT PRIMARY KEY REFERENCES scenarios(id), station TEXT NOT NULL,
            base_revision INTEGER NOT NULL, model_version TEXT NOT NULL, projected_json TEXT NOT NULL,
            constraints_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
        connection.execute("CREATE TABLE IF NOT EXISTS assumptions (id INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT NOT NULL REFERENCES scenarios(id), name TEXT NOT NULL, value_json TEXT NOT NULL, unit TEXT NOT NULL, assumption_type TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS remote_operation_proposals (id TEXT PRIMARY KEY, station TEXT NOT NULL, scenario_id TEXT NOT NULL REFERENCES scenarios(id), title TEXT NOT NULL, base_revision INTEGER NOT NULL, model_version TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL)")
        connection.execute("""CREATE TABLE IF NOT EXISTS weather_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, station_id TEXT NOT NULL, observation_time TEXT NOT NULL,
            temperature_c REAL, pressure_hpa REAL, relative_humidity_pct REAL, wind_speed REAL,
            wind_direction_deg REAL, source TEXT NOT NULL, source_type TEXT NOT NULL, quality TEXT NOT NULL,
            fetched_at TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(station_id, source_type, observation_time))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS remote_operations (
            id TEXT PRIMARY KEY, station_id TEXT NOT NULL, operation_type TEXT NOT NULL, title TEXT NOT NULL,
            description TEXT NOT NULL, priority TEXT NOT NULL, status TEXT NOT NULL, created_by TEXT NOT NULL,
            created_by_role TEXT NOT NULL, assigned_role TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            scenario_id TEXT, base_revision INTEGER NOT NULL, model_version TEXT, expires_at TEXT, review_by TEXT,
            provenance_json TEXT NOT NULL, metadata_json TEXT NOT NULL, sync_state TEXT NOT NULL DEFAULT 'PENDING_SYNC',
            idempotency_key TEXT, UNIQUE(station_id, idempotency_key))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS operation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL REFERENCES remote_operations(id),
            event_type TEXT NOT NULL, actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, timestamp TEXT NOT NULL,
            previous_state TEXT, new_state TEXT NOT NULL, station_revision INTEGER NOT NULL,
            payload_hash TEXT NOT NULL, source TEXT NOT NULL, metadata_json TEXT NOT NULL)""")
        connection.execute("CREATE TABLE IF NOT EXISTS operation_evidence (id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES remote_operations(id), source_type TEXT NOT NULL, label TEXT NOT NULL, uri TEXT, digest TEXT, added_by TEXT NOT NULL, added_at TEXT NOT NULL, metadata_json TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS operation_reports (id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES remote_operations(id), report_type TEXT NOT NULL, reported_by TEXT NOT NULL, reported_role TEXT NOT NULL, reported_at TEXT NOT NULL, station_revision INTEGER NOT NULL, observations_json TEXT NOT NULL, evidence_ids_json TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'PENDING')")
        connection.execute("CREATE TABLE IF NOT EXISTS station_observations (id TEXT PRIMARY KEY, station_id TEXT NOT NULL REFERENCES stations(id), operation_id TEXT REFERENCES remote_operations(id), metric TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL, observation_time TEXT NOT NULL, recorded_at TEXT NOT NULL, method TEXT NOT NULL, source_type TEXT NOT NULL, quality TEXT NOT NULL, confidence TEXT NOT NULL, notes TEXT NOT NULL, recorded_by TEXT NOT NULL, station_revision INTEGER NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS operation_reviews (operation_id TEXT PRIMARY KEY REFERENCES remote_operations(id), reviewed_by TEXT NOT NULL, reviewed_at TEXT NOT NULL, classification TEXT NOT NULL, notes TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS station_sync_events (idempotency_key TEXT PRIMARY KEY, station_id TEXT NOT NULL, operation_id TEXT NOT NULL, action TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL)")
        connection.execute("""CREATE TABLE IF NOT EXISTS sync_event_log (
            event_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, device_id TEXT NOT NULL, station_id TEXT NOT NULL,
            operation_id TEXT NOT NULL, event_type TEXT NOT NULL, created_at TEXT NOT NULL, local_revision INTEGER NOT NULL,
            payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL, sync_status TEXT NOT NULL DEFAULT 'PENDING',
            attempt_count INTEGER NOT NULL DEFAULT 0, last_attempt_at TEXT, server_event_id TEXT, server_revision INTEGER,
            error_code TEXT, error_message TEXT)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS sync_conflicts (
            id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE REFERENCES sync_event_log(event_id), station_id TEXT NOT NULL,
            operation_id TEXT NOT NULL, client_revision INTEGER NOT NULL, server_revision INTEGER NOT NULL,
            server_state TEXT NOT NULL, conflict_type TEXT NOT NULL, resolution TEXT, resolved_by TEXT, resolved_at TEXT, resolution_note TEXT)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS reconciliations (
            id TEXT PRIMARY KEY, station_id TEXT NOT NULL, scenario_id TEXT, operation_id TEXT NOT NULL UNIQUE REFERENCES remote_operations(id),
            created_at TEXT NOT NULL, reviewed_at TEXT, reviewed_by TEXT, status TEXT NOT NULL, projection_snapshot TEXT NOT NULL,
            observation_snapshot TEXT NOT NULL, comparison_result TEXT NOT NULL, review_classification TEXT, review_notes TEXT NOT NULL DEFAULT '',
            current_revision INTEGER NOT NULL, model_version TEXT, follow_up_operation_id TEXT)""")
        connection.execute("CREATE TABLE IF NOT EXISTS reconciliation_events (id INTEGER PRIMARY KEY AUTOINCREMENT, reconciliation_id TEXT NOT NULL REFERENCES reconciliations(id), event_type TEXT NOT NULL, actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, occurred_at TEXT NOT NULL, payload_json TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS technical_findings (id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES remote_operations(id), station_id TEXT NOT NULL, technical_user_id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('OPEN','COMPLETE','BLOCKED')), finding TEXT NOT NULL, observations_json TEXT NOT NULL, constraints_text TEXT NOT NULL, recommendation TEXT NOT NULL, blocked_reason TEXT NOT NULL DEFAULT '', evidence_refs_json TEXT NOT NULL, provenance_json TEXT NOT NULL, revision INTEGER NOT NULL, demo_run_id TEXT)")
        connection.execute("CREATE TABLE IF NOT EXISTS technical_finding_events (id TEXT PRIMARY KEY, finding_id TEXT NOT NULL REFERENCES technical_findings(id), event_type TEXT NOT NULL, actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, occurred_at TEXT NOT NULL, previous_status TEXT, new_status TEXT NOT NULL, revision INTEGER NOT NULL, source TEXT NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS demo_runs (run_id TEXT PRIMARY KEY, station_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS demo_records (run_id TEXT NOT NULL REFERENCES demo_runs(run_id), entity_type TEXT NOT NULL, record_id TEXT NOT NULL, PRIMARY KEY(run_id, entity_type, record_id))")
        connection.execute("CREATE TABLE IF NOT EXISTS sync_gateway_control (id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)")
        connection.execute("INSERT OR IGNORE INTO sync_gateway_control(id,status,updated_at,updated_by) VALUES(1,'CONNECTED',?,?)", (datetime.now(timezone.utc).isoformat(), "system"))
        connection.execute("CREATE TABLE IF NOT EXISTS edge_gateway_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, station_id TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE, idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL, received_at TEXT NOT NULL, response_json TEXT NOT NULL)")
        for table, column in (("measurement_requests", "scenario_id TEXT"), ("measurement_requests", "model_version TEXT"), ("inspection_requests", "scenario_id TEXT"), ("inspection_requests", "model_version TEXT")):
            try:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
            except sqlite3.OperationalError as error:
                if "duplicate column" not in str(error).lower():
                    raise
        migrate_legacy_operations(connection)


def authorized_hq(authorization: str | None, station: str | None = None) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sign in to access the HQ workspace")
    token = authorization.removeprefix("Bearer ").strip()
    with database() as connection:
        session = connection.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    if session is None:
        raise HTTPException(status_code=401, detail="Session is invalid or expired")
    if session["role"] != "hq":
        raise HTTPException(status_code=403, detail="HQ Operations Analyst role required")
    if station is not None and session["station"] != station:
        raise HTTPException(status_code=403, detail="Station does not match the authenticated session")
    return session


def authorized_session(authorization: str | None, station: str | None = None, allowed_roles: set[str] | None = None) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sign in to access remote operations")
    token = authorization.removeprefix("Bearer ").strip()
    with database() as connection:
        session = connection.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    if session is None:
        raise HTTPException(status_code=401, detail="Session is invalid or expired")
    if allowed_roles is not None and session["role"] not in allowed_roles:
        raise HTTPException(status_code=403, detail="This role is not authorized for this operation")
    if station is not None and session["station"] != station:
        raise HTTPException(status_code=403, detail="Station does not match the authenticated session")
    return session


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    station: str = Field(min_length=1)
    role: str = Field(min_length=1)


class UserSession(BaseModel):
    token: str
    username: str
    role: str
    station: str
    created_at: str


class MeasurementRequestInput(BaseModel):
    metric: str = Field(min_length=1, max_length=120)
    unit: str = Field(min_length=1, max_length=30)
    reason: str = Field(min_length=1, max_length=1000)
    deadline: str = Field(min_length=1)
    related_scenario: str | None = None
    scenario_id: str | None = None
    model_version: str | None = None
    base_revision: int = Field(ge=0)


class InspectionRequestInput(BaseModel):
    asset: str = Field(min_length=1, max_length=120)
    requested_checks: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=1000)
    deadline: str = Field(min_length=1)
    base_revision: int = Field(ge=0)
    scenario_id: str | None = None
    model_version: str | None = None


class ScenarioParameters(BaseModel):
    chp_available: bool
    fuel_consumption_lph: float = Field(ge=0, le=5000)
    fuel_inflow_lph: float = Field(default=0, ge=0, le=5000)
    critical_load_kw: float = Field(ge=0, le=1000)
    deferrable_load_kw: float = Field(ge=0, le=1000)
    outdoor_temperature_c: float = Field(ge=-100, le=30)
    water_demand_l_per_day: float = Field(ge=0, le=50000)
    duration_hours: float = Field(ge=1, le=168)
    thermal_supply_kwth: float | None = Field(default=None, ge=0, le=5000)
    thermal_demand_kwth: float | None = Field(default=None, ge=0, le=5000)


class ScenarioInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_revision: int = Field(ge=0)
    model_version: str = Field(min_length=1)
    parameters: ScenarioParameters
    weather_reference: dict | None = None


class ProposalInput(BaseModel):
    scenario_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=160)
    base_revision: int = Field(ge=0)
    model_version: str = Field(min_length=1)


class RemoteOperationInput(BaseModel):
    operation_type: Literal["MEASUREMENT_REQUEST", "INSPECTION_REQUEST", "ACTION_PLAN", "SCENARIO_FOLLOWUP"]
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    priority: Literal["P1", "P2", "P3"] = "P2"
    assigned_role: Literal["station", "technical"] = "station"
    base_revision: int = Field(ge=0)
    scenario_id: str | None = None
    model_version: str | None = None
    expires_at: str | None = None
    review_by: str | None = None
    provenance: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


class OperationActionInput(BaseModel):
    note: str = Field(default="", max_length=2000)


class OperationReportInput(BaseModel):
    observations: dict = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=2000)
    acknowledge_revision: bool = False


class StationObservationInput(BaseModel):
    metric: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=80)
    unit: str = Field(min_length=1, max_length=30)
    observation_time: str = Field(min_length=1, max_length=80)
    method: str = Field(min_length=1, max_length=120)
    quality: str = "UNASSESSED"
    confidence: str = "UNASSESSED"
    notes: str = Field(default="", max_length=2000)


class OutcomeReviewInput(BaseModel):
    classification: Literal["CONSISTENT", "PARTIALLY_CONSISTENT", "NOT_CONSISTENT", "INSUFFICIENT_EVIDENCE"]
    notes: str = Field(default="", max_length=2000)


class StationSyncInput(BaseModel):
    operation_id: str = Field(min_length=1, max_length=80)
    action: Literal["seen", "accept", "decline", "clarification", "progress", "report-complete", "report-blocked", "observation"]
    payload: dict = Field(default_factory=dict)


class SyncBatchEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=1, max_length=160)
    operation_id: str = Field(min_length=1, max_length=80)
    event_type: Literal["seen", "accept", "decline", "clarification", "progress", "report-complete", "report-blocked", "observation", "technical-observation", "technical-finding-create", "technical-finding-disposition"]
    created_at: str
    local_revision: int = Field(ge=0)
    payload: dict = Field(default_factory=dict)
    payload_hash: str | None = None


class SyncBatchInput(BaseModel):
    station_id: str
    device_id: str = Field(min_length=1, max_length=120)
    base_server_revision: int = Field(ge=0)
    events: list[SyncBatchEvent] = Field(max_length=200)


class GatewayBatchInput(SyncBatchInput):
    pass


class GatewayControlInput(BaseModel):
    status: Literal["CONNECTED", "DEGRADED", "OFFLINE"]


class GatewayAckInput(BaseModel):
    station_id: str
    operation_ids: list[str] = Field(max_length=200)


class ReconciliationReviewInput(BaseModel):
    classification: Literal["CONSISTENT_WITH_PROJECTION", "PARTIALLY_CONSISTENT", "DIFFERS_FROM_PROJECTION", "INSUFFICIENT_EVIDENCE", "NOT_COMPARABLE"]
    notes: str = Field(min_length=1, max_length=4000)
    follow_up_action: str = Field(default="", max_length=1000)


class ReconciliationFollowUpInput(BaseModel):
    operation_type: Literal["MEASUREMENT_REQUEST", "INSPECTION_REQUEST", "ACTION_PLAN", "SCENARIO_FOLLOWUP"] = "SCENARIO_FOLLOWUP"
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=4000)
    priority: Literal["P1", "P2", "P3"] = "P2"
    metric: str | None = None
    unit: str | None = None
    asset: str | None = None
    requested_checks: str | None = None
    candidate_action: str | None = None


class ConflictResolutionInput(BaseModel):
    resolution: Literal["KEEP_SERVER_STATE", "APPLY_LOCAL_EVENT", "CREATE_FOLLOW_UP", "MARK_FOR_HQ_REVIEW"]
    note: str = Field(default="", max_length=2000)


class OperationEvidenceInput(BaseModel):
    source_type: Literal["DOCUMENTED", "HISTORICAL", "DEMO_SYNTHETIC", "SCENARIO_ASSUMPTION", "MANUAL", "NCPOR_OFFICIAL_OBSERVATION"]
    label: str = Field(min_length=1, max_length=200)
    uri: str | None = None
    digest: str | None = None
    metadata: dict = Field(default_factory=dict)


class TechnicalFindingInput(BaseModel):
    finding: str = Field(min_length=5, max_length=4000)
    observations: dict = Field(default_factory=dict)
    constraints: str = Field(default="", max_length=2000)
    recommendation: str = Field(default="", max_length=2000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)


class TechnicalFindingDispositionInput(BaseModel):
    summary: str = Field(min_length=5, max_length=2000)
    reason: str = Field(default="", max_length=2000)


def _weather_document(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    payload = json.loads(row["payload_json"])
    payload["id"] = row["id"]
    return payload


def _latest_official_weather(connection: sqlite3.Connection, station_id: str) -> dict | None:
    row = connection.execute("SELECT * FROM weather_observations WHERE station_id = ? AND source_type = 'NCPOR_OFFICIAL_OBSERVATION' ORDER BY observation_time DESC, id DESC LIMIT 1", (station_id,)).fetchone()
    return _weather_document(row)


@app.get("/api/weather/official/{station_id}")
def get_official_weather(station_id: str, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"hq", "station", "technical"})
    if station_id not in {"maitri", "bharati"}:
        raise HTTPException(status_code=404, detail="Unknown station")
    if session["station"] != station_id:
        raise HTTPException(status_code=403, detail="Station does not match the authenticated session")
    with database() as connection:
        observation = _latest_official_weather(connection, station_id)
    return {"status": "AVAILABLE" if observation else "UNAVAILABLE", "label": "NCPOR Official Observation", "observation": observation, "message": None if observation else "NCPOR observation unavailable. Refresh to request the latest public observation."}


@app.post("/api/weather/official/{station_id}/refresh")
def refresh_official_weather(station_id: str, authorization: str | None = Header(default=None), force: bool = Query(default=False)) -> dict:
    session = authorized_session(authorization, allowed_roles={"hq", "station", "technical"})
    if station_id not in {"maitri", "bharati"}:
        raise HTTPException(status_code=404, detail="Unknown station")
    if session["station"] != station_id:
        raise HTTPException(status_code=403, detail="Station does not match the authenticated session")
    with database() as connection:
        cached = _latest_official_weather(connection, station_id)
    if cached and not force:
        try:
            fetched_at = datetime.fromisoformat(cached["fetched_at"].replace("Z", "+00:00"))
            age_seconds = max(0, int((datetime.now(timezone.utc) - fetched_at).total_seconds()))
        except (KeyError, TypeError, ValueError):
            age_seconds = None
        if age_seconds is not None and age_seconds < 600:
            return {"status": "AVAILABLE", "label": "NCPOR Official Observation", "observation": cached,
                    "message": None, "refreshed": False, "cache_age_seconds": age_seconds}
    try:
        observation = NCPOR_PROVIDER.fetch_station_observation(station_id)
        with database() as connection:
            connection.execute("""INSERT INTO weather_observations (station_id, observation_time, temperature_c, pressure_hpa, relative_humidity_pct, wind_speed, wind_direction_deg, source, source_type, quality, fetched_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(station_id, source_type, observation_time) DO UPDATE SET temperature_c=excluded.temperature_c, pressure_hpa=excluded.pressure_hpa, relative_humidity_pct=excluded.relative_humidity_pct, wind_speed=excluded.wind_speed, wind_direction_deg=excluded.wind_direction_deg, quality=excluded.quality, fetched_at=excluded.fetched_at, payload_json=excluded.payload_json""", (observation["station_id"], observation["observation_time"], observation.get("temperature_c"), observation.get("pressure_hpa"), observation.get("relative_humidity_pct"), observation.get("wind_speed"), observation.get("wind_direction_deg"), "NCPOR", "NCPOR_OFFICIAL_OBSERVATION", observation["quality"], observation["fetched_at"], json.dumps(observation, sort_keys=True)))
            cached = _latest_official_weather(connection, station_id)
        return {"status": "AVAILABLE", "label": "NCPOR Official Observation", "observation": cached,
                "message": None, "refreshed": True, "cache_age_seconds": 0}
    except Exception as error:
        with database() as connection:
            cached = _latest_official_weather(connection, station_id)
        return {"status": "LAST_NCPOR_OBSERVATION" if cached else "UNAVAILABLE", "label": "Last NCPOR observation" if cached else "NCPOR observation unavailable", "observation": cached, "message": str(error) if cached else "NCPOR observation unavailable. No cached observation exists.", "refreshed": False}


@app.get("/api/weather/historical/{station_id}")
def get_historical_weather(station_id: str, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"hq", "station", "technical"})
    if station_id not in {"maitri", "bharati"}:
        raise HTTPException(status_code=404, detail="Unknown station")
    if session["station"] != station_id:
        raise HTTPException(status_code=403, detail="Station does not match the authenticated session")
    observations = load_local_historical(station_id)
    return {"station_id": station_id, "status": "CONFIGURED" if observations else "NOT_CONFIGURED", "label": "Historical replay data", "source_type": "NCPOR_HISTORICAL", "observations": observations}


def append_operation_event(connection: sqlite3.Connection, operation: sqlite3.Row, session: sqlite3.Row, event_type: str, previous: str | None, new: str, payload: dict, source: str = "LOCAL_API") -> None:
    now = datetime.now(timezone.utc).isoformat()
    station_rev = station_revision(connection, operation["station_id"])
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    connection.execute("INSERT INTO operation_events (operation_id, event_type, actor_id, actor_role, timestamp, previous_state, new_state, station_revision, payload_hash, source, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (operation["id"], event_type, session["username"], session["role"], now, previous, new, station_rev, payload_hash, source, payload_json))


def create_operation_row(connection: sqlite3.Connection, session: sqlite3.Row, operation_id: str, operation_type: str, title: str, description: str, priority: str, assigned_role: str, base_revision: int, scenario_id: str | None, model_version: str | None, expires_at: str | None, review_by: str | None, provenance: dict, metadata: dict, idempotency_key: str | None) -> tuple[sqlite3.Row, bool]:
    existing = None
    if idempotency_key:
        existing = connection.execute("SELECT * FROM remote_operations WHERE station_id = ? AND idempotency_key = ?", (session["station"], idempotency_key)).fetchone()
    if existing:
        if (existing["operation_type"] != operation_type or existing["title"] != title or existing["description"] != description
                or existing["priority"] != priority or existing["assigned_role"] != assigned_role
                or existing["base_revision"] != base_revision or existing["scenario_id"] != scenario_id
                or existing["model_version"] != model_version or existing["expires_at"] != expires_at
                or existing["review_by"] != review_by or json.loads(existing["provenance_json"]) != provenance
                or json.loads(existing["metadata_json"]) != metadata):
            raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different operation payload")
        return existing, False
    now = datetime.now(timezone.utc).isoformat()
    connection.execute("INSERT INTO remote_operations (id, station_id, operation_type, title, description, priority, status, created_by, created_by_role, assigned_role, created_at, updated_at, scenario_id, base_revision, model_version, expires_at, review_by, provenance_json, metadata_json, sync_state, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_SYNC', ?)", (operation_id, session["station"], operation_type, title, description, priority, session["username"], session["role"], assigned_role, now, now, scenario_id, base_revision, model_version, expires_at, review_by, json.dumps(provenance, sort_keys=True), json.dumps(metadata, sort_keys=True), idempotency_key))
    row = connection.execute("SELECT * FROM remote_operations WHERE id = ?", (operation_id,)).fetchone()
    append_operation_event(connection, row, session, "OPERATION_CREATED", None, "DRAFT", {"operation_type": operation_type, "scenario_id": scenario_id, "base_revision": base_revision, "model_version": model_version}, "LOCAL_API")
    return row, True


def migrate_legacy_operations(connection: sqlite3.Connection) -> None:
    """Expose existing request/proposal rows through the canonical audited operation stream."""
    imports = []
    for row in connection.execute("SELECT * FROM measurement_requests").fetchall():
        imports.append((dict(row), "MEASUREMENT_REQUEST", row["metric"], row["reason"], "station", {"metric": row["metric"], "unit": row["unit"], "deadline": row["deadline"]}, row["scenario_id"], row["model_version"]))
    for row in connection.execute("SELECT * FROM inspection_requests").fetchall():
        imports.append((dict(row), "INSPECTION_REQUEST", row["asset"], row["reason"], "station", {"asset": row["asset"], "requested_checks": row["requested_checks"], "deadline": row["deadline"]}, row["scenario_id"], row["model_version"]))
    for row in connection.execute("SELECT * FROM remote_operation_proposals").fetchall():
        imports.append((dict(row), "ACTION_PLAN", row["title"], "Scenario-linked non-executable proposal.", "station", {"scenario_id": row["scenario_id"]}, row["scenario_id"], row["model_version"]))
    for data, kind, title, description, assigned, metadata, scenario_id, model_version in imports:
        if connection.execute("SELECT 1 FROM remote_operations WHERE id = ?", (data["id"],)).fetchone():
            continue
        legacy_session = {"station": data["station"], "username": data.get("created_by", "legacy-import"), "role": "hq"}
        row, created = create_operation_row(connection, legacy_session, data["id"], kind, title, description, "P2", assigned, data["base_revision"], scenario_id, model_version, data.get("deadline"), None, {"source_type": "MANUAL", "legacy_import": True}, metadata, data["id"])
        old_status = data.get("status", "DRAFT")
        if created and old_status == "QUEUED":
            now = datetime.now(timezone.utc).isoformat()
            connection.execute("UPDATE remote_operations SET status = 'QUEUED', updated_at = ? WHERE id = ?", (now, data["id"]))
            queued = connection.execute("SELECT * FROM remote_operations WHERE id = ?", (data["id"],)).fetchone()
            append_operation_event(connection, queued, legacy_session, "LEGACY_QUEUED_REQUEST_IMPORTED", "DRAFT", "QUEUED", {"delivery": "PENDING_SYNC"}, "LEGACY_DATABASE")


def operation_document(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    document = dict(row)
    document["provenance"] = json.loads(document.pop("provenance_json"))
    document["metadata"] = json.loads(document.pop("metadata_json"))
    document["timeline"] = [dict(event) for event in connection.execute("SELECT * FROM operation_events WHERE operation_id = ? ORDER BY id", (row["id"],)).fetchall()]
    report = connection.execute("SELECT * FROM operation_reports WHERE operation_id = ? ORDER BY reported_at DESC LIMIT 1", (row["id"],)).fetchone()
    document["latest_report"] = dict(report) if report else None
    if document["latest_report"]:
        document["latest_report"]["observations"] = json.loads(document["latest_report"].pop("observations_json"))
        document["latest_report"]["evidence_ids"] = json.loads(document["latest_report"].pop("evidence_ids_json"))
    document["evidence"] = [dict(item) for item in connection.execute("SELECT * FROM operation_evidence WHERE operation_id = ? ORDER BY added_at", (row["id"],)).fetchall()]
    review = connection.execute("SELECT * FROM operation_reviews WHERE operation_id = ?", (row["id"],)).fetchone()
    document["outcome_review"] = dict(review) if review else None
    document["station_observations"] = [dict(item) for item in connection.execute("SELECT * FROM station_observations WHERE operation_id = ? ORDER BY recorded_at", (row["id"],)).fetchall()]
    return document


def operation_for_session(connection: sqlite3.Connection, operation_id: str, session: sqlite3.Row) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM remote_operations WHERE id = ? AND station_id = ?", (operation_id, session["station"])).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Operation not found for this station")
    if session["role"] == "station" and row["assigned_role"] not in {"station", "both"}:
        raise HTTPException(status_code=403, detail="Operation is not assigned to the station role")
    if session["role"] == "technical" and row["assigned_role"] not in {"technical", "both"}:
        raise HTTPException(status_code=403, detail="Operation is not assigned to the technical role")
    return row


def ensure_operation_revision(connection: sqlite3.Connection, row: sqlite3.Row) -> int:
    current = station_revision(connection, row["station_id"])
    if current != row["base_revision"]:
        raise HTTPException(status_code=409, detail=f"STALE BASE REVISION: operation references REV {row['base_revision']}; station is now at REV {current}. Refresh state and create a new operation.")
    return current


def transition_operation(operation_id: str, authorization: str | None, allowed_roles: set[str], expected_states: set[str], new_state: str, event_type: str, payload: dict | None = None) -> dict:
    session = authorized_session(authorization, allowed_roles=allowed_roles)
    payload = payload or {}
    with database() as connection:
        row = operation_for_session(connection, operation_id, session)
        if row["status"] not in expected_states:
            raise HTTPException(status_code=409, detail=f"Cannot transition {row['status']} to {new_state}.")
        if session["role"] == "hq" and new_state in {"REVIEWED_FOR_COMMUNICATION", "QUEUED"}:
            ensure_operation_revision(connection, row)
        now = datetime.now(timezone.utc).isoformat()
        connection.execute("UPDATE remote_operations SET status = ?, updated_at = ?, sync_state = CASE WHEN ? = 'QUEUED' THEN 'PENDING_SYNC' ELSE sync_state END WHERE id = ?", (new_state, now, new_state, operation_id))
        updated = connection.execute("SELECT * FROM remote_operations WHERE id = ?", (operation_id,)).fetchone()
        append_operation_event(connection, updated, session, event_type, row["status"], new_state, payload)
        if new_state == "QUEUED":
            if operation_id.startswith("MR-"):
                connection.execute("UPDATE measurement_requests SET status = 'QUEUED' WHERE id = ?", (operation_id,))
            elif operation_id.startswith("IR-"):
                connection.execute("UPDATE inspection_requests SET status = 'QUEUED' WHERE id = ?", (operation_id,))
    with database() as connection:
        return operation_document(connection, connection.execute("SELECT * FROM remote_operations WHERE id = ?", (operation_id,)).fetchone())


def record_operation_delivery(connection: sqlite3.Connection, operation_id: str, actor_id: str = "transport", actor_role: str = "transport", metadata: dict | None = None) -> None:
    """Transport integration hook. Never called until an actual delivery is confirmed."""
    row = connection.execute("SELECT * FROM remote_operations WHERE id = ?", (operation_id,)).fetchone()
    if row is None or row["status"] != "QUEUED":
        raise ValueError("Only queued operations may be marked delivered")
    now = datetime.now(timezone.utc).isoformat()
    connection.execute("UPDATE remote_operations SET status = 'DELIVERED', sync_state = 'SYNCED', updated_at = ? WHERE id = ?", (now, operation_id))
    pseudo_actor = {"username": actor_id, "role": actor_role}
    append_operation_event(connection, row, pseudo_actor, "OPERATION_DELIVERED", "QUEUED", "DELIVERED", metadata or {}, "CONFIRMED_TRANSPORT")


@app.get("/api/operations")
def list_operations(authorization: str | None = Header(default=None), status: str | None = None) -> dict:
    session = authorized_session(authorization, allowed_roles={"hq", "station", "technical"})
    with database() as connection:
        revision = station_revision(connection, session["station"])
        rows = connection.execute("SELECT * FROM remote_operations WHERE station_id = ? ORDER BY updated_at DESC", (session["station"],)).fetchall()
        if session["role"] == "station":
            rows = [row for row in rows if row["assigned_role"] in {"station", "both"} and row["status"] not in {"DRAFT", "REVIEWED_FOR_COMMUNICATION"}]
        elif session["role"] == "technical":
            rows = [row for row in rows if row["assigned_role"] in {"technical", "both"} and row["status"] not in {"DRAFT", "REVIEWED_FOR_COMMUNICATION"}]
        if status:
            rows = [row for row in rows if row["status"] == status]
        documents = [operation_document(connection, row) for row in rows]
        state = connection.execute("SELECT value_json FROM station_state WHERE station = ? AND field = 'connectivity'", (session["station"],)).fetchone()
        connectivity = json.loads(state["value_json"]) if state else "UNKNOWN"
    return {"station_id": session["station"], "current_revision": revision, "connectivity": connectivity, "connectivity_source": "DEMO_SYNTHETIC" if connectivity != "UNKNOWN" else "UNKNOWN", "operations": documents}


@app.post("/api/station/operations/{operation_id}/receive")
def receive_station_operation(operation_id: str, authorization: str | None = Header(default=None)) -> dict:
    session=authorized_session(authorization,allowed_roles={"station"})
    with database() as connection:
        row=operation_for_session(connection,operation_id,session)
        if row["status"]=="DELIVERED": return operation_document(connection,row)
        if row["status"]!="QUEUED": raise HTTPException(status_code=409,detail=f"Only a queued operation can be received; current status is {row['status']}.")
        record_operation_delivery(connection,operation_id,actor_id=session["username"],actor_role=session["role"],metadata={"transport":"station inbox API request","delivery_confirmation":"station client received operation envelope"})
        delivered=connection.execute("SELECT * FROM remote_operations WHERE id=?",(operation_id,)).fetchone()
        return operation_document(connection,delivered)


@app.get("/api/operations/{operation_id}")
def get_operation(operation_id: str, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"hq", "station", "technical"})
    with database() as connection:
        row = operation_for_session(connection, operation_id, session)
        if session["role"] == "station" and row["assigned_role"] not in {"station", "both"}:
            raise HTTPException(status_code=403, detail="Operation is not assigned to the station role")
        if session["role"] == "technical" and row["assigned_role"] not in {"technical", "both"}:
            raise HTTPException(status_code=403, detail="Operation is not assigned to the technical role")
        return operation_document(connection, row)


@app.get("/api/operations/{operation_id}/timeline")
def operation_timeline(operation_id: str, authorization: str | None = Header(default=None)) -> list[dict]:
    session = authorized_session(authorization, allowed_roles={"hq", "station", "technical"})
    with database() as connection:
        operation_for_session(connection, operation_id, session)
        rows = connection.execute("SELECT * FROM operation_events WHERE operation_id = ? ORDER BY id", (operation_id,)).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/operations")
def create_remote_operation(payload: RemoteOperationInput, authorization: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
    session = authorized_session(authorization, allowed_roles={"hq"})
    if not idempotency_key or len(idempotency_key) > 120:
        raise HTTPException(status_code=400, detail="Provide an Idempotency-Key to safely retry operation creation")
    with database() as connection:
        replay = connection.execute("SELECT * FROM remote_operations WHERE station_id = ? AND idempotency_key = ?", (session["station"], idempotency_key)).fetchone()
        if replay:
            if (replay["operation_type"] != payload.operation_type or replay["title"] != payload.title
                    or replay["description"] != payload.description or replay["priority"] != payload.priority
                    or replay["assigned_role"] != payload.assigned_role or replay["base_revision"] != payload.base_revision
                    or replay["scenario_id"] != payload.scenario_id or replay["model_version"] != payload.model_version
                    or replay["expires_at"] != payload.expires_at or replay["review_by"] != payload.review_by
                    or json.loads(replay["provenance_json"]) != payload.provenance
                    or json.loads(replay["metadata_json"]) != payload.metadata):
                raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different operation payload")
            return {**operation_document(connection, replay), "idempotent_replay": True}
        current = station_revision(connection, session["station"])
        if payload.base_revision != current:
            raise HTTPException(status_code=409, detail=f"STALE BASE REVISION: refresh station state (current REV {current}).")
        if payload.scenario_id:
            scenario = connection.execute("SELECT base_revision, model_version FROM scenarios WHERE id = ? AND station = ?", (payload.scenario_id, session["station"])).fetchone()
            if scenario is None or scenario["base_revision"] != payload.base_revision or scenario["model_version"] != payload.model_version:
                raise HTTPException(status_code=409, detail="Scenario reference does not match this station, revision, and model version.")
        prefixes = {"MEASUREMENT_REQUEST": "MR", "INSPECTION_REQUEST": "IR", "ACTION_PLAN": "ROP", "SCENARIO_FOLLOWUP": "SF"}
        operation_id = f"{prefixes[payload.operation_type]}-{secrets.token_hex(5).upper()}"
        if payload.operation_type == "ACTION_PLAN" and not payload.scenario_id:
            raise HTTPException(status_code=422, detail="Action plans must reference the scenario that produced them")
        row, created = create_operation_row(connection, session, operation_id, payload.operation_type, payload.title, payload.description, payload.priority, payload.assigned_role, payload.base_revision, payload.scenario_id, payload.model_version, payload.expires_at, payload.review_by, payload.provenance, payload.metadata, idempotency_key)
        if created and payload.operation_type == "MEASUREMENT_REQUEST":
            metric, unit = payload.metadata.get("metric"), payload.metadata.get("unit")
            if not metric or not unit:
                raise HTTPException(status_code=422, detail="Measurement requests require metadata.metric and metadata.unit")
            connection.execute("INSERT INTO measurement_requests (id, station, metric, unit, reason, deadline, related_scenario, base_revision, status, created_at, created_by, scenario_id, model_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?)", (operation_id, session["station"], metric, unit, payload.description, payload.expires_at or "", payload.scenario_id, payload.base_revision, row["created_at"], session["username"], payload.scenario_id, payload.model_version))
        elif created and payload.operation_type == "INSPECTION_REQUEST":
            asset, checks = payload.metadata.get("asset"), payload.metadata.get("requested_checks")
            if not asset or not checks:
                raise HTTPException(status_code=422, detail="Inspection requests require metadata.asset and metadata.requested_checks")
            connection.execute("INSERT INTO inspection_requests (id, station, asset, requested_checks, reason, deadline, base_revision, status, created_at, created_by, scenario_id, model_version) VALUES (?, ?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?)", (operation_id, session["station"], asset, checks, payload.description, payload.expires_at or "", payload.base_revision, row["created_at"], session["username"], payload.scenario_id, payload.model_version))
        elif created and payload.operation_type == "ACTION_PLAN":
            connection.execute("INSERT INTO remote_operation_proposals (id, station, scenario_id, title, base_revision, model_version, status, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?)", (operation_id, session["station"], payload.scenario_id, payload.title, payload.base_revision, payload.model_version or "unknown", row["created_at"], session["username"]))
        document = operation_document(connection, row)
    return {**document, "idempotent_replay": not created}


@app.post("/api/operations/{operation_id}/review")
def review_operation(operation_id: str, payload: OutcomeReviewInput | OperationActionInput, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"hq"})
    with database() as connection:
        row = operation_for_session(connection, operation_id, session)
    if row["status"] in {"REPORTED_COMPLETE", "REPORTED_BLOCKED"}:
        classification = getattr(payload, "classification", "INSUFFICIENT_EVIDENCE")
        result = transition_operation(operation_id, authorization, {"hq"}, {row["status"]}, "REVIEWED_OUTCOME", "OUTCOME_REVIEWED", {"note": payload.notes if hasattr(payload, "notes") else payload.note, "classification": classification, "verification": "HQ_REVIEW_OF_STATION_REPORT"})
        with database() as connection:
            connection.execute("UPDATE operation_reports SET review_status = 'HQ_REVIEWED' WHERE operation_id = ? AND review_status = 'PENDING'", (operation_id,))
            connection.execute("INSERT INTO operation_reviews (operation_id, reviewed_by, reviewed_at, classification, notes) VALUES (?, ?, ?, ?, ?) ON CONFLICT(operation_id) DO UPDATE SET reviewed_by=excluded.reviewed_by, reviewed_at=excluded.reviewed_at, classification=excluded.classification, notes=excluded.notes", (operation_id, session["username"], datetime.now(timezone.utc).isoformat(), classification, payload.notes if hasattr(payload, "notes") else payload.note))
            result["outcome_review"] = dict(connection.execute("SELECT * FROM operation_reviews WHERE operation_id = ?", (operation_id,)).fetchone())
        result["review_semantics"] = "Station-reported outcome reviewed by HQ; this does not claim independent verification."
        return result
    return transition_operation(operation_id, authorization, {"hq"}, {"DRAFT"}, "REVIEWED_FOR_COMMUNICATION", "REVIEWED_FOR_COMMUNICATION", {"note": payload.note})


@app.post("/api/operations/{operation_id}/clarification-response")
def respond_to_operation_clarification(operation_id: str, payload: OperationActionInput, authorization: str | None = Header(default=None)) -> dict:
    return transition_operation(operation_id, authorization, {"hq"}, {"CLARIFICATION_REQUESTED"}, "REVIEWED_FOR_COMMUNICATION", "CLARIFICATION_RESPONSE_REVIEWED", {"note": payload.note})


@app.post("/api/operations/{operation_id}/queue")
def queue_remote_operation(operation_id: str, authorization: str | None = Header(default=None)) -> dict:
    return transition_operation(operation_id, authorization, {"hq"}, {"REVIEWED_FOR_COMMUNICATION", "DRAFT"}, "QUEUED", "OPERATION_QUEUED", {"delivery": "PENDING_SYNC"})


@app.post("/api/operations/{operation_id}/seen")
def mark_operation_seen(operation_id: str, authorization: str | None = Header(default=None)) -> dict:
    return transition_operation(operation_id, authorization, {"station"}, {"DELIVERED"}, "SEEN", "OPERATION_SEEN")


@app.post("/api/operations/{operation_id}/accept")
def accept_operation(operation_id: str, payload: OperationActionInput, authorization: str | None = Header(default=None)) -> dict:
    return transition_operation(operation_id, authorization, {"station"}, {"DELIVERED", "SEEN"}, "ACCEPTED", "OPERATION_ACCEPTED", {"note": payload.note})


@app.post("/api/operations/{operation_id}/decline")
def decline_operation(operation_id: str, payload: OperationActionInput, authorization: str | None = Header(default=None)) -> dict:
    return transition_operation(operation_id, authorization, {"station"}, {"DELIVERED", "SEEN"}, "DECLINED", "OPERATION_DECLINED", {"note": payload.note})


@app.post("/api/operations/{operation_id}/clarification")
def clarify_operation(operation_id: str, payload: OperationActionInput, authorization: str | None = Header(default=None)) -> dict:
    return transition_operation(operation_id, authorization, {"station"}, {"DELIVERED", "SEEN"}, "CLARIFICATION_REQUESTED", "CLARIFICATION_REQUESTED", {"note": payload.note})


@app.post("/api/operations/{operation_id}/progress")
def mark_operation_in_progress(operation_id: str, payload: OperationActionInput, authorization: str | None = Header(default=None)) -> dict:
    return transition_operation(operation_id, authorization, {"station"}, {"ACCEPTED"}, "IN_PROGRESS", "STATION_REPORTED_IN_PROGRESS", {"note": payload.note, "verification": "STATION_REPORTED"})


@app.post("/api/operations/{operation_id}/report-complete")
def report_operation_complete(operation_id: str, payload: OperationReportInput, authorization: str | None = Header(default=None)) -> dict:
    return _report_operation(operation_id, payload, authorization, "REPORTED_COMPLETE")


@app.post("/api/operations/{operation_id}/report-blocked")
def report_operation_blocked(operation_id: str, payload: OperationReportInput, authorization: str | None = Header(default=None)) -> dict:
    return _report_operation(operation_id, payload, authorization, "REPORTED_BLOCKED")


def _report_operation(operation_id: str, payload: OperationReportInput, authorization: str | None, new_status: str) -> dict:
    session = authorized_session(authorization, allowed_roles={"station"})
    with database() as connection:
        row = operation_for_session(connection, operation_id, session)
        if row["status"] not in {"ACCEPTED", "IN_PROGRESS"}:
            raise HTTPException(status_code=409, detail="An operation must be accepted before a station outcome can be reported")
        current_revision = station_revision(connection, row["station_id"])
        if current_revision != row["base_revision"] and not payload.acknowledge_revision:
            raise HTTPException(status_code=409, detail=f"STATE CHANGED SINCE REQUEST: base REV {row['base_revision']}; current REV {current_revision}. Acknowledge the current revision before reporting.")
        now = datetime.now(timezone.utc).isoformat()
        report_id = f"OR-{secrets.token_hex(5).upper()}"
        connection.execute("INSERT INTO operation_reports (id, operation_id, report_type, reported_by, reported_role, reported_at, station_revision, observations_json, evidence_ids_json, review_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')", (report_id, operation_id, new_status, session["username"], session["role"], now, station_revision(connection, row["station_id"]), json.dumps(payload.observations, sort_keys=True), json.dumps(payload.evidence_ids)))
        updated_at = now
        connection.execute("UPDATE remote_operations SET status = ?, updated_at = ? WHERE id = ?", (new_status, updated_at, operation_id))
        updated = connection.execute("SELECT * FROM remote_operations WHERE id = ?", (operation_id,)).fetchone()
        append_operation_event(connection, updated, session, "STATION_OUTCOME_REPORTED", row["status"], new_status, {"report_id": report_id, "note": payload.note, "verification": "STATION_REPORTED_NOT_INDEPENDENTLY_VERIFIED"})
    with database() as connection:
        return operation_document(connection, connection.execute("SELECT * FROM remote_operations WHERE id = ?", (operation_id,)).fetchone())


@app.get("/api/station/observations")
def station_observation_list(authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"station", "technical"})
    with database() as connection:
        observations = [dict(row) for row in connection.execute("SELECT * FROM station_observations WHERE station_id = ? ORDER BY recorded_at DESC", (session["station"],)).fetchall()]
    return {"station_id": session["station"], "observations": observations}


@app.post("/api/station/operations/{operation_id}/observations")
def create_station_observation(operation_id: str, payload: StationObservationInput, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"station", "technical"})
    with database() as connection:
        operation = operation_for_session(connection, operation_id, session)
        revision = station_revision(connection, session["station"])
        observation_id = f"OBS-{secrets.token_hex(5).upper()}"
        now = datetime.now(timezone.utc).isoformat()
        connection.execute("INSERT INTO station_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'MANUAL', ?, ?, ?, ?, ?)", (observation_id, session["station"], operation_id, payload.metric, payload.value, payload.unit, payload.observation_time, now, payload.method, payload.quality, payload.confidence, payload.notes, session["username"], revision))
        append_operation_event(connection, operation, session, "STATION_OBSERVATION_RECORDED", operation["status"], operation["status"], {"observation_id": observation_id, "metric": payload.metric, "source_type": "MANUAL"})
        return {"id": observation_id, "station_id": session["station"], "operation_id": operation_id, **payload.model_dump(), "source_type": "MANUAL", "recorded_at": now, "recorded_by": session["username"], "station_revision": revision}


@app.get("/api/station/state")
def station_state_view(authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"station", "technical"})
    with database() as connection:
        rows = connection.execute("SELECT * FROM station_state WHERE station = ?", (session["station"],)).fetchall()
        current_revision = station_revision(connection, session["station"])
    return {"station_id": session["station"], "current_revision": current_revision, "values": [{"field": row["field"], "value": json.loads(row["value_json"]), "unit": row["unit"], "provenance": row["source_kind"], "quality": row["quality"], "timestamp": row["timestamp"]} for row in rows]}


@app.post("/api/station/sync")
def sync_station_event(payload: StationSyncInput, authorization: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
    session = authorized_session(authorization, allowed_roles={"station"})
    if not idempotency_key or len(idempotency_key) > 120:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required for station event synchronization")
    with database() as connection:
        prior = connection.execute("SELECT * FROM station_sync_events WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if prior:
            if prior["station_id"] != session["station"] or prior["operation_id"] != payload.operation_id or prior["action"] != payload.action:
                raise HTTPException(status_code=409, detail="Idempotency key was already used for a different station event")
            return {**json.loads(prior["response_json"]), "idempotent_replay": True}
    action_payload = OperationActionInput(note=str(payload.payload.get("note", "")))
    if payload.action == "seen": result = transition_operation(payload.operation_id, authorization, {"station"}, {"DELIVERED"}, "SEEN", "OPERATION_SEEN")
    elif payload.action == "accept": result = transition_operation(payload.operation_id, authorization, {"station"}, {"DELIVERED", "SEEN"}, "ACCEPTED", "OPERATION_ACCEPTED", {"note": action_payload.note})
    elif payload.action == "decline": result = transition_operation(payload.operation_id, authorization, {"station"}, {"DELIVERED", "SEEN"}, "DECLINED", "OPERATION_DECLINED", {"note": action_payload.note})
    elif payload.action == "clarification": result = transition_operation(payload.operation_id, authorization, {"station"}, {"DELIVERED", "SEEN"}, "CLARIFICATION_REQUESTED", "CLARIFICATION_REQUESTED", {"note": action_payload.note})
    elif payload.action == "progress": result = transition_operation(payload.operation_id, authorization, {"station"}, {"ACCEPTED"}, "IN_PROGRESS", "STATION_REPORTED_IN_PROGRESS", {"note": action_payload.note, "verification": "STATION_REPORTED"})
    elif payload.action == "observation":
        result = create_station_observation(payload.operation_id, StationObservationInput.model_validate(payload.payload), authorization)
    else:
        report = OperationReportInput.model_validate(payload.payload)
        result = _report_operation(payload.operation_id, report, authorization, "REPORTED_COMPLETE" if payload.action == "report-complete" else "REPORTED_BLOCKED")
    with database() as connection:
        connection.execute("INSERT OR IGNORE INTO station_sync_events VALUES (?, ?, ?, ?, ?, ?)", (idempotency_key, session["station"], payload.operation_id, payload.action, json.dumps(result), datetime.now(timezone.utc).isoformat()))
    return result


def sync_log_document(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    return result


def apply_edge_technical_event(item: SyncBatchEvent, station: sqlite3.Row) -> dict:
    """Apply technical station work at HQ only after its Edge outbox event arrives."""
    payload = item.payload
    actor_id = str(payload.get("actor_id") or "technical_engineer")
    with database() as connection:
        operation = connection.execute("SELECT * FROM remote_operations WHERE id=? AND station_id=?", (item.operation_id, station["station"])).fetchone()
        if operation is None:
            raise HTTPException(status_code=404, detail="Technical event references an unknown station operation")
        if operation["assigned_role"] not in {"technical", "both"}:
            raise HTTPException(status_code=403, detail="Technical event is not assigned to this operation")
        revision = station_revision(connection, station["station"])
        if item.event_type == "technical-observation":
            observed = StationObservationInput.model_validate(payload)
            observation_id = str(payload.get("observation_id") or f"OBS-{secrets.token_hex(5).upper()}")
            connection.execute("INSERT OR IGNORE INTO station_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'MANUAL', ?, ?, ?, ?, ?)",
                (observation_id,station["station"],item.operation_id,observed.metric,observed.value,observed.unit,observed.observation_time,
                 datetime.now(timezone.utc).isoformat(),observed.method,observed.quality,observed.confidence,observed.notes,actor_id,revision))
            append_operation_event(connection,operation,{"username":actor_id,"role":"technical"},"TECHNICAL_OBSERVATION_RECORDED",operation["status"],operation["status"],
                {"observation_id":observation_id,"metric":observed.metric,"source_type":"MANUAL"},"STATION_EDGE")
            result={"observation_id":observation_id,"operation_id":item.operation_id,"source_type":"MANUAL","station_revision":revision}
        elif item.event_type == "technical-finding-create":
            finding_id = str(payload.get("finding_id") or f"TF-{secrets.token_hex(6).upper()}")
            finding = TechnicalFindingInput.model_validate(payload)
            existing=connection.execute("SELECT * FROM technical_findings WHERE id=?",(finding_id,)).fetchone()
            if existing:
                return technical_finding_document(connection,existing)
            run=connection.execute("SELECT run_id FROM demo_records WHERE entity_type='OPERATION' AND record_id=? ORDER BY run_id LIMIT 1",(item.operation_id,)).fetchone()
            run_id=run["run_id"] if run else None
            connection.execute("INSERT INTO technical_findings (id,operation_id,station_id,technical_user_id,created_at,updated_at,status,finding,observations_json,constraints_text,recommendation,blocked_reason,evidence_refs_json,provenance_json,revision,demo_run_id) VALUES (?,?,?,?,?,?,'OPEN',?,?,?,?,?,?,?, ?,?)",
                (finding_id,item.operation_id,station["station"],actor_id,datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat(),finding.finding.strip(),json.dumps(finding.observations,sort_keys=True),finding.constraints,finding.recommendation,"",json.dumps(finding.evidence_refs),json.dumps({"source_type":"MANUAL","station_edge_event":item.event_id,"station_id":station["station"]},sort_keys=True),revision,run_id))
            if run_id:
                connection.execute("INSERT OR IGNORE INTO demo_records (run_id,entity_type,record_id) VALUES (?,'TECHNICAL_FINDING',?)",(run_id,finding_id))
            append_technical_finding_event(connection,finding_id,{"username":actor_id,"role":"technical"},"TECHNICAL_FINDING_CREATED",None,"OPEN",revision,{"operation_id":item.operation_id,"source":"STATION_EDGE","event_id":item.event_id})
            saved=connection.execute("SELECT * FROM technical_findings WHERE id=?",(finding_id,)).fetchone()
            result=technical_finding_document(connection,saved)
        else:
            finding_id=str(payload.get("finding_id") or "")
            finding=connection.execute("SELECT * FROM technical_findings WHERE id=? AND station_id=? AND operation_id=?",(finding_id,station["station"],item.operation_id)).fetchone()
            if finding is None:
                raise HTTPException(status_code=404,detail="Technical finding does not exist at HQ")
            if finding["technical_user_id"] != actor_id or finding["status"] != "OPEN":
                raise HTTPException(status_code=409,detail="Finding author or OPEN status does not match the local disposition")
            new_status=str(payload.get("status") or "")
            if new_status not in {"COMPLETE","BLOCKED"}:
                raise HTTPException(status_code=422,detail="Unsupported technical finding disposition")
            reason=str(payload.get("reason") or "").strip()
            summary=str(payload.get("summary") or "").strip()
            if new_status=="BLOCKED" and len(reason)<5 or new_status=="COMPLETE" and len(summary)<5:
                raise HTTPException(status_code=422,detail="Technical finding disposition requires a clear reason or completion summary")
            updated=datetime.now(timezone.utc).isoformat()
            blocked_reason=reason if new_status=="BLOCKED" else ""
            finding_text=summary if new_status=="COMPLETE" else finding["finding"]
            connection.execute("UPDATE technical_findings SET status=?,finding=?,blocked_reason=?,updated_at=?,revision=? WHERE id=? AND status='OPEN'",(new_status,finding_text,blocked_reason,updated,revision,finding_id))
            append_technical_finding_event(connection,finding_id,{"username":actor_id,"role":"technical"},f"TECHNICAL_FINDING_{new_status}","OPEN",new_status,revision,{"summary":summary,"reason":blocked_reason,"source":"STATION_EDGE","event_id":item.event_id})
            result=technical_finding_document(connection,connection.execute("SELECT * FROM technical_findings WHERE id=?",(finding_id,)).fetchone())
        connection.execute("INSERT OR IGNORE INTO station_sync_events VALUES (?,?,?,?,?,?)",(item.idempotency_key,station["station"],item.operation_id,item.event_type,json.dumps(result,sort_keys=True),datetime.now(timezone.utc).isoformat()))
    return result


@app.post("/api/sync/batch")
def sync_batch(payload: SyncBatchInput, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, station=payload.station_id, allowed_roles={"station"})
    results: dict[str, list] = {"accepted": [], "already_applied": [], "conflicts": [], "rejected": []}
    # Preserve the durable client queue order; this is the causal order for one device batch.
    events = payload.events
    for item in events:
        body = {"operation_id": item.operation_id, "action": item.event_type, "payload": item.payload}
        body_json = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(body_json.encode("utf-8")).hexdigest()
        if item.payload_hash and item.payload_hash != digest:
            results["rejected"].append({"event_id": item.event_id, "status": "REJECTED", "error_code": "PAYLOAD_HASH_MISMATCH"})
            continue
        with database() as connection:
            prior = connection.execute("SELECT * FROM sync_event_log WHERE idempotency_key = ? OR event_id = ?", (item.idempotency_key, item.event_id)).fetchone()
            if prior:
                applied=connection.execute("SELECT response_json FROM station_sync_events WHERE idempotency_key=?",(item.idempotency_key,)).fetchone()
                if applied and prior["station_id"]==session["station"] and prior["operation_id"]==item.operation_id and prior["payload_hash"]==digest:
                    timeline=json.loads(applied["response_json"]).get("timeline") or []
                    server_event_id=str(timeline[-1]["id"]) if timeline else item.event_id
                    connection.execute("UPDATE sync_event_log SET sync_status='SYNCED',server_event_id=?,server_revision=?,error_code=NULL,error_message=NULL WHERE event_id=?",(server_event_id,station_revision(connection,session["station"]),prior["event_id"]))
                    results["already_applied"].append({"event_id":item.event_id,"status":"ALREADY_APPLIED","server_event_id":server_event_id,"server_revision":station_revision(connection,session["station"])})
                    continue
                if prior["payload_hash"] != digest or prior["operation_id"] != item.operation_id or prior["station_id"] != session["station"]:
                    results["rejected"].append({"event_id": item.event_id, "status": "REJECTED", "error_code": "IDEMPOTENCY_PAYLOAD_MISMATCH"})
                    continue
                elif prior["sync_status"] == "SYNCED":
                    results["already_applied"].append({"event_id": item.event_id, "status": "ALREADY_APPLIED", "server_event_id": prior["server_event_id"], "server_revision": prior["server_revision"]})
                    continue
                elif prior["sync_status"] == "CONFLICT":
                    results["conflicts"].append({"event_id": item.event_id, "status": "REVISION_CONFLICT", "client_revision": prior["local_revision"], "server_revision": prior["server_revision"], "operation_id": item.operation_id})
                    continue
                elif prior["sync_status"] in {"PENDING", "FAILED", "REJECTED", "BLOCKED_PENDING_PREDECESSOR", "SYNCING"}:
                    connection.execute("UPDATE sync_event_log SET sync_status='SYNCING',attempt_count=attempt_count+1,last_attempt_at=?,error_code=NULL,error_message=NULL WHERE event_id=?",(datetime.now(timezone.utc).isoformat(),prior["event_id"]))
                else:
                    results["rejected"].append({"event_id": item.event_id, "status": prior["sync_status"], "error_code": prior["error_code"]})
                    continue
            if not prior:
                connection.execute("INSERT INTO sync_event_log (event_id,idempotency_key,device_id,station_id,operation_id,event_type,created_at,local_revision,payload_hash,payload_json,sync_status,attempt_count,last_attempt_at) VALUES (?,?,?,?,?,?,?,?,?,?,'SYNCING',1,?)", (item.event_id, item.idempotency_key, payload.device_id, session["station"], item.operation_id, item.event_type, item.created_at, item.local_revision, digest, body_json, datetime.now(timezone.utc).isoformat()))
            current_revision = station_revision(connection, session["station"])
        if item.local_revision != current_revision:
            conflict_id = f"CF-{secrets.token_hex(5).upper()}"
            with database() as connection:
                connection.execute("UPDATE sync_event_log SET sync_status='CONFLICT',server_revision=?,error_code='REVISION_CONFLICT',error_message=? WHERE event_id=?", (current_revision, f"Station revision advanced from {item.local_revision} to {current_revision} while offline.", item.event_id))
                operation = connection.execute("SELECT status FROM remote_operations WHERE id=? AND station_id=?", (item.operation_id, session["station"])).fetchone()
                connection.execute("INSERT INTO sync_conflicts (id,event_id,station_id,operation_id,client_revision,server_revision,server_state,conflict_type) VALUES (?,?,?,?,?,?,?,?)", (conflict_id,item.event_id,session["station"],item.operation_id,item.local_revision,current_revision,operation["status"] if operation else "UNKNOWN","REVISION_CONFLICT"))
            results["conflicts"].append({"event_id":item.event_id,"conflict_id":conflict_id,"status":"REVISION_CONFLICT","client_revision":item.local_revision,"server_revision":current_revision,"operation_id":item.operation_id})
            continue
        try:
            if item.event_type in {"technical-observation", "technical-finding-create", "technical-finding-disposition"}:
                accepted = apply_edge_technical_event(item, session)
            else:
                accepted = sync_station_event(StationSyncInput(**body), authorization, item.idempotency_key)
            timeline = accepted.get("timeline") or []
            server_event_id = str(timeline[-1]["id"]) if timeline else item.event_id
            with database() as connection:
                server_revision = station_revision(connection, session["station"])
            with database() as connection:
                connection.execute("UPDATE sync_event_log SET sync_status='SYNCED',server_event_id=?,server_revision=?,error_code=NULL,error_message=NULL WHERE event_id=?", (server_event_id,server_revision,item.event_id))
            results["accepted"].append({"event_id":item.event_id,"status":"SYNCED","server_event_id":server_event_id,"server_revision":server_revision})
        except HTTPException as error:
            status = "BLOCKED_PENDING_PREDECESSOR" if error.status_code == 409 else "REJECTED"
            with database() as connection:
                connection.execute("UPDATE sync_event_log SET sync_status=?,server_revision=?,error_code=?,error_message=? WHERE event_id=?", (status,station_revision(connection,session["station"]),status,str(error.detail),item.event_id))
            bucket = "rejected" if status == "REJECTED" else "conflicts"
            results[bucket].append({"event_id":item.event_id,"status":status,"error_code":status,"error_message":str(error.detail),"operation_id":item.operation_id})
        except ValidationError as error:
            with database() as connection:
                connection.execute("UPDATE sync_event_log SET sync_status='REJECTED',error_code='INVALID_EVENT_PAYLOAD',error_message=? WHERE event_id=?",(str(error),item.event_id))
            results["rejected"].append({"event_id":item.event_id,"status":"REJECTED","error_code":"INVALID_EVENT_PAYLOAD"})
    with database() as connection:
        server_revision=station_revision(connection,session["station"])
    return {"station_id":session["station"],"device_id":payload.device_id,"server_revision":server_revision,**results}


def gateway_status_value(connection: sqlite3.Connection) -> dict:
    row = connection.execute("SELECT status,updated_at,updated_by FROM sync_gateway_control WHERE id=1").fetchone()
    return dict(row) if row else {"status":"CONNECTED","updated_at":None,"updated_by":"UNKNOWN"}


def require_gateway_token(token: str | None):
    if not token or not secrets.compare_digest(token, os.environ.get("PRATIBIMB_GATEWAY_TOKEN", SYNC_GATEWAY_TOKEN)):
        raise HTTPException(status_code=403, detail="Invalid sync gateway credential")


def require_gateway_connected():
    with database() as connection:
        state = gateway_status_value(connection)
    if state["status"] == "OFFLINE":
        raise HTTPException(status_code=503, detail="Sync gateway link is OFFLINE; local edge state remains available")
    return state


@app.get("/api/gateway/status")
def gateway_status(authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"hq"})
    with database() as connection:
        state = gateway_status_value(connection)
        pending = connection.execute("SELECT COUNT(*) FROM remote_operations WHERE station_id=? AND status='QUEUED'", (session["station"],)).fetchone()[0]
        received = connection.execute("SELECT COUNT(*) FROM sync_event_log WHERE station_id=? AND sync_status='SYNCED'", (session["station"],)).fetchone()[0]
        latest = connection.execute("SELECT MAX(last_attempt_at) FROM sync_event_log WHERE station_id=? AND sync_status='SYNCED'", (session["station"],)).fetchone()[0]
    return {**state,"station_id":session["station"],"queued_hq_operations":pending,"received_station_events":received,"last_sync_at":latest,"demo_controls_enabled":os.environ.get("PRATIBIMB_DEMO_MODE", "0") == "1"}


@app.post("/api/gateway/control")
def gateway_control(payload: GatewayControlInput, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"hq"})
    if session["username"] != "admin_hq" or os.environ.get("PRATIBIMB_DEMO_MODE", "0") != "1":
        raise HTTPException(status_code=403, detail="Transport controls are available only to the local demo administrator")
    now = datetime.now(timezone.utc).isoformat()
    with database() as connection:
        connection.execute("UPDATE sync_gateway_control SET status=?,updated_at=?,updated_by=? WHERE id=1", (payload.status,now,session["username"]))
    return {"status":payload.status,"updated_at":now,"updated_by":session["username"],"transport_boundary":"HQ sync gateway HTTP endpoint"}


@app.post("/api/gateway/ingest")
def gateway_ingest(payload: GatewayBatchInput, x_sync_token: str | None = Header(default=None, alias="X-Sync-Token")) -> dict:
    require_gateway_token(x_sync_token)
    require_gateway_connected()
    machine_token = hashlib.sha256(f"edge-session:{x_sync_token}:{payload.station_id}".encode()).hexdigest()
    with database() as connection:
        connection.execute("INSERT OR IGNORE INTO sessions(token,username,role,station,created_at) VALUES(?,?,'station',?,?)", (machine_token,f"edge-{payload.station_id}",payload.station_id,datetime.now(timezone.utc).isoformat()))
    result = sync_batch(payload, f"Bearer {machine_token}")
    with database() as connection:
        for item in payload.events:
            encoded = json.dumps({"event_id":item.event_id,"idempotency_key":item.idempotency_key,"operation_id":item.operation_id,"event_type":item.event_type}, sort_keys=True)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            connection.execute("INSERT OR IGNORE INTO edge_gateway_audit(station_id,event_id,idempotency_key,payload_hash,received_at,response_json) VALUES(?,?,?,?,?,?)", (payload.station_id,item.event_id,item.idempotency_key,digest,datetime.now(timezone.utc).isoformat(),json.dumps(result,sort_keys=True)))
    return {**result,"transport":"GATEWAY_HTTP","received_by_hq":True}


@app.get("/api/gateway/outbox/{station_id}")
def gateway_outbox(station_id: str, x_sync_token: str | None = Header(default=None, alias="X-Sync-Token")) -> dict:
    require_gateway_token(x_sync_token)
    require_gateway_connected()
    if station_id not in {"maitri","bharati"}:
        raise HTTPException(status_code=404, detail="Unknown station")
    with database() as connection:
        rows = connection.execute("SELECT * FROM remote_operations WHERE station_id=? AND status='QUEUED' ORDER BY created_at", (station_id,)).fetchall()
        operations = [operation_document(connection,row) for row in rows]
    return {"station_id":station_id,"operations":operations,"source":"HQ_OUTBOX"}


@app.post("/api/gateway/ack")
def gateway_ack(payload: GatewayAckInput, x_sync_token: str | None = Header(default=None, alias="X-Sync-Token")) -> dict:
    require_gateway_token(x_sync_token)
    require_gateway_connected()
    if payload.station_id not in {"maitri","bharati"}:
        raise HTTPException(status_code=404, detail="Unknown station")
    delivered=[]
    with database() as connection:
        for operation_id in payload.operation_ids:
            row=connection.execute("SELECT * FROM remote_operations WHERE id=? AND station_id=?",(operation_id,payload.station_id)).fetchone()
            if row is None or row["status"] not in {"QUEUED","DELIVERED"}:
                continue
            if row["status"] == "QUEUED":
                record_operation_delivery(connection,operation_id,"station-edge","station",{"transport":"HQ_EDGE_SYNC_GATEWAY","delivery_confirmation":"persisted in station edge store"})
            delivered.append(operation_id)
    return {"station_id":payload.station_id,"delivered":delivered}


@app.get("/api/gateway/events")
def gateway_events(authorization: str | None = Header(default=None)) -> dict:
    session=authorized_session(authorization,allowed_roles={"hq"})
    with database() as connection:
        rows=connection.execute("SELECT event_id,idempotency_key,operation_id,event_type,created_at,local_revision,payload_hash,payload_json,sync_status,server_revision,error_code,error_message FROM sync_event_log WHERE station_id=? ORDER BY created_at DESC LIMIT 250",(session["station"],)).fetchall()
        control=gateway_status_value(connection)
    return {"station_id":session["station"],"transport":control,"events":[{**dict(row),"payload":json.loads(row["payload_json"])} for row in rows]}


@app.get("/api/sync/status")
def sync_status(authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"station", "technical", "hq"})
    with database() as connection:
        grouped = {row["sync_status"]:row["count"] for row in connection.execute("SELECT sync_status,COUNT(*) AS count FROM sync_event_log WHERE station_id=? GROUP BY sync_status", (session["station"],)).fetchall()}
        latest = connection.execute("SELECT last_attempt_at,server_revision FROM sync_event_log WHERE station_id=? AND sync_status='SYNCED' ORDER BY last_attempt_at DESC LIMIT 1", (session["station"],)).fetchone()
    return {"station_id":session["station"],"last_successful_sync":latest["last_attempt_at"] if latest else None,"last_server_revision":latest["server_revision"] if latest else None,"counts":grouped}


@app.get("/api/sync/queue")
def sync_queue(authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"station", "technical", "hq"})
    with database() as connection:
        rows = connection.execute("SELECT * FROM sync_event_log WHERE station_id=? ORDER BY created_at DESC LIMIT 300", (session["station"],)).fetchall()
    return {"station_id":session["station"],"events":[sync_log_document(row) for row in rows]}


@app.get("/api/sync/conflicts")
def sync_conflicts(authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"station", "hq"})
    with database() as connection:
        rows = connection.execute("SELECT c.*,e.event_type,e.payload_json,e.created_at FROM sync_conflicts c JOIN sync_event_log e ON e.event_id=c.event_id WHERE c.station_id=? AND c.resolution IS NULL ORDER BY e.created_at DESC", (session["station"],)).fetchall()
    return {"station_id":session["station"],"conflicts":[{**dict(row),"payload":json.loads(row["payload_json"])} for row in rows]}


@app.post("/api/sync/conflicts/{conflict_id}/resolve")
def resolve_sync_conflict(conflict_id: str, payload: ConflictResolutionInput, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"station", "hq"})
    with database() as connection:
        row=connection.execute("SELECT * FROM sync_conflicts WHERE id=? AND station_id=?",(conflict_id,session["station"])).fetchone()
        if row is None: raise HTTPException(status_code=404,detail="Sync conflict not found")
        connection.execute("UPDATE sync_conflicts SET resolution=?,resolved_by=?,resolved_at=?,resolution_note=? WHERE id=?",(payload.resolution,session["username"],datetime.now(timezone.utc).isoformat(),payload.note,conflict_id))
        if payload.resolution == "MARK_FOR_HQ_REVIEW":
            connection.execute("UPDATE sync_event_log SET sync_status='CONFLICT',error_code='MARKED_FOR_HQ_REVIEW' WHERE event_id=?",(row["event_id"],))
        elif payload.resolution == "KEEP_SERVER_STATE":
            connection.execute("UPDATE sync_event_log SET sync_status='REJECTED',error_code='KEEP_SERVER_STATE' WHERE event_id=?",(row["event_id"],))
        elif payload.resolution == "APPLY_LOCAL_EVENT":
            old_event=connection.execute("SELECT * FROM sync_event_log WHERE event_id=?",(row["event_id"],)).fetchone()
            replacement_id=f"EVT-{secrets.token_hex(8).upper()}";replacement_key=f"{old_event['device_id']}-{replacement_id}"
            current=station_revision(connection,session["station"])
            body=json.loads(old_event["payload_json"])
            if body.get("action") in {"report-complete","report-blocked"}:
                body.setdefault("payload",{})["acknowledge_revision"]=True
            body_json=json.dumps(body,sort_keys=True,separators=(",",":"),ensure_ascii=False)
            digest=hashlib.sha256(body_json.encode("utf-8")).hexdigest()
            connection.execute("UPDATE sync_event_log SET sync_status='REJECTED',error_code='REPLACED_AFTER_HUMAN_CONFLICT_REVIEW' WHERE event_id=?",(row["event_id"],))
            connection.execute("INSERT INTO sync_event_log (event_id,idempotency_key,device_id,station_id,operation_id,event_type,created_at,local_revision,payload_hash,payload_json,sync_status,attempt_count) VALUES (?,?,?,?,?,?,?,?,?,?,'PENDING',0)",(replacement_id,replacement_key,old_event["device_id"],session["station"],old_event["operation_id"],old_event["event_type"],datetime.now(timezone.utc).isoformat(),current,digest,body_json))
        result=dict(connection.execute("SELECT * FROM sync_conflicts WHERE id=?",(conflict_id,)).fetchone())
        if payload.resolution=="APPLY_LOCAL_EVENT":
            new=connection.execute("SELECT * FROM sync_event_log WHERE idempotency_key=?",(replacement_key,)).fetchone();result["replacement_event"]=sync_log_document(new)
        result["resolution_note"]=payload.note
        return result


def build_projection_comparison(scenario: dict | None, observations: list[dict], operation_model_version: str | None = None) -> dict:
    if not scenario:
        return {"status":"NOT_COMPARABLE","items":[],"reason":"No scenario is linked to this operation."}
    projected = (scenario.get("result") or {}).get("projected") or {}
    inputs = scenario.get("inputs") or {}
    scenario_version = scenario.get("model_version")
    result_version = (scenario.get("result") or {}).get("model_version")
    if not scenario_version or result_version != scenario_version or (operation_model_version and operation_model_version != scenario_version):
        return {"status":"NOT_COMPARABLE","items":[],"reason":"Scenario, stored result, and linked operation model versions do not match."}
    try:
        started = datetime.fromisoformat(str(scenario["created_at"]).replace("Z", "+00:00"))
        duration = float(inputs.get("duration_hours", 24))
    except (KeyError, TypeError, ValueError):
        started, duration = None, None
    mapping = {
        "indoor temperature": ("heat_indoor_c", "°C"), "outdoor temperature": ("outdoor_temperature_c", "°C"),
        "fuel level": ("fuel_l", "L"), "fuel reserve": ("fuel_l", "L"), "water storage": ("water_l", "L"),
        "power availability": ("power_available_kw", "kW"), "power available": ("power_available_kw", "kW"),
        "power balance": ("power_balance_kw", "kW"), "power deficit": ("power_deficit_kw", "kW"),
        "water production": ("water_production_lph", "L/h"), "water production rate": ("water_production_lph", "L/h"),
    }
    comparisons=[]
    for obs in observations:
        metric=str(obs["metric"]).strip().lower()
        if obs.get("station_id") and obs.get("station_id") != scenario.get("station", scenario.get("station_id")):
            comparisons.append({"type":"NON_COMPARABLE","metric":obs["metric"],"reason":"Observation and scenario belong to different stations."})
            continue
        if started is None or duration is None:
            comparisons.append({"type":"NON_COMPARABLE","metric":obs["metric"],"reason":"Scenario comparison time window is missing or invalid."})
            continue
        try:
            observed_at=datetime.fromisoformat(str(obs["observation_time"]).replace("Z", "+00:00"))
            if observed_at.tzinfo is None:
                observed_at=observed_at.replace(tzinfo=timezone.utc)
            if started.tzinfo is None:
                started=started.replace(tzinfo=timezone.utc)
            if observed_at < started or observed_at > started + timedelta(hours=duration):
                comparisons.append({"type":"NON_COMPARABLE","metric":obs["metric"],"reason":"Observation timestamp is outside the scenario comparison window."})
                continue
        except (KeyError, TypeError, ValueError):
            comparisons.append({"type":"NON_COMPARABLE","metric":obs["metric"],"reason":"Observation timestamp is missing or invalid."})
            continue
        quality=str(obs.get("quality", "UNKNOWN")).upper()
        if quality not in {"GOOD", "VALID", "VERIFIED", "HIGH"}:
            comparisons.append({"type":"NON_COMPARABLE","metric":obs["metric"],"quality":quality,"reason":"Observation quality is not sufficient for a direct comparison."})
            continue
        if metric == "chp availability" and isinstance(inputs.get("chp_available"), bool):
            expected="Available" if inputs["chp_available"] else "Unavailable"
            comparisons.append({"type":"STATE_COMPARISON","metric":obs["metric"],"scenario_assumption":expected,"station_reported":obs["value"],"unit":obs["unit"],"quality":quality,"status":"COMPARABLE","provenance":"SCENARIO_ASSUMPTION / MANUAL"})
            continue
        pair=mapping.get(metric)
        if not pair:
            comparisons.append({"type":"NON_COMPARABLE","metric":obs["metric"],"station_reported":obs["value"],"unit":obs["unit"],"reason":"The scenario contains no matching projected metric."})
            continue
        field,expected_unit=pair
        if str(obs["unit"]).strip().lower() != expected_unit.lower() or projected.get(field) is None:
            comparisons.append({"type":"NON_COMPARABLE","metric":obs["metric"],"station_reported":obs["value"],"unit":obs["unit"],"reason":"Projection is missing or units are not compatible."})
            continue
        try:
            projected_value=float(projected[field]); observed_value=float(obs["value"])
        except (TypeError,ValueError):
            comparisons.append({"type":"NON_COMPARABLE","metric":obs["metric"],"station_reported":obs["value"],"unit":obs["unit"],"reason":"One or both values are not numeric."})
            continue
        absolute=abs(observed_value-projected_value)
        relative=(absolute/abs(projected_value)*100) if projected_value != 0 and expected_unit not in {"°C", "°F"} else None
        item={"type":"DIRECT_COMPARISON","metric":obs["metric"],"projected_value":projected_value,"observed_value":observed_value,"unit":expected_unit,"absolute_difference":absolute,"relative_difference_pct":relative,"quality":quality,"status":"COMPARABLE","provenance":"MODEL_OUTPUT / MANUAL","uncertainty_status":"NOT_MODELED"}
        ranges=(scenario.get("result") or {}).get("uncertainty") or {}
        envelope=ranges.get(field) if isinstance(ranges,dict) else None
        if isinstance(envelope,dict) and isinstance(envelope.get("lower"),(int,float)) and isinstance(envelope.get("upper"),(int,float)):
            item["expected_range"]={"lower":envelope["lower"],"upper":envelope["upper"]}
            item["uncertainty_status"]="WITHIN_RANGE" if envelope["lower"] <= observed_value <= envelope["upper"] else "OUTSIDE_RANGE"
        comparisons.append(item)
    comparable=sum(item["type"]!="NON_COMPARABLE" for item in comparisons)
    status="NOT_COMPARABLE" if comparable==0 else "PARTIALLY_COMPARABLE" if comparable<len(comparisons) else "COMPARABLE"
    return {"status":status,"items":comparisons,"reason":None if comparable else (comparisons[0].get("reason") if comparisons else "No station observations were provided."),"scenario_id":scenario.get("id"),"model_version":scenario_version,"comparison_window":{"start":scenario.get("created_at"),"duration_hours":duration}}


def reconciliation_document(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    result=dict(row)
    for column in ("projection_snapshot","observation_snapshot","comparison_result"):
        result[column]=json.loads(result[column])
    result["events"]=[{**dict(event),"payload":json.loads(event["payload_json"])} for event in connection.execute("SELECT * FROM reconciliation_events WHERE reconciliation_id=? ORDER BY id",(row["id"],)).fetchall()]
    return result


@app.get("/api/reconciliation")
def list_reconciliations(authorization: str | None = Header(default=None), status: str | None = None) -> dict:
    session=authorized_session(authorization,allowed_roles={"hq"})
    with database() as connection:
        rows=connection.execute("SELECT * FROM reconciliations WHERE station_id=? ORDER BY created_at DESC",(session["station"],)).fetchall()
        if status: rows=[row for row in rows if row["status"]==status]
        cases=[]
        for row in rows:
            op=connection.execute("SELECT title,status,updated_at FROM remote_operations WHERE id=?",(row["operation_id"],)).fetchone()
            scen=connection.execute("SELECT name FROM scenarios WHERE id=?",(row["scenario_id"],)).fetchone() if row["scenario_id"] else None
            cases.append({**reconciliation_document(connection,row),"operation_title":op["title"],"operation_status":op["status"],"scenario_name":scen["name"] if scen else None,"last_updated":op["updated_at"]})
    return {"station_id":session["station"],"reconciliations":cases}


@app.post("/api/reconciliation/{operation_id}/create")
def create_reconciliation(operation_id: str, authorization: str | None = Header(default=None)) -> dict:
    session=authorized_session(authorization,allowed_roles={"hq"})
    with database() as connection:
        operation=connection.execute("SELECT * FROM remote_operations WHERE id=? AND station_id=?",(operation_id,session["station"])).fetchone()
        if operation is None: raise HTTPException(status_code=404,detail="Operation not found for this station")
        if operation["status"] not in {"REPORTED_COMPLETE","REPORTED_BLOCKED","REVIEWED_OUTCOME","CLOSED","REOPENED"}: raise HTTPException(status_code=409,detail="Station outcome is not ready for reconciliation")
        prior=connection.execute("SELECT * FROM reconciliations WHERE operation_id=?",(operation_id,)).fetchone()
        if prior: return reconciliation_document(connection,prior)
        scenario_row=connection.execute("SELECT * FROM scenarios WHERE id=? AND station=?",(operation["scenario_id"],session["station"])).fetchone() if operation["scenario_id"] else None
        scenario=None
        if scenario_row:
            raw_inputs=json.loads(scenario_row["inputs_json"])
            scenario={"id":scenario_row["id"],"station":scenario_row["station"],"name":scenario_row["name"],"base_revision":scenario_row["base_revision"],"model_version":scenario_row["model_version"],"created_at":scenario_row["created_at"],"inputs":raw_inputs.get("parameters",raw_inputs),"weather_reference":raw_inputs.get("weather_reference"),"baseline":json.loads(scenario_row["baseline_json"]),"result":json.loads(scenario_row["result_json"]),"status":scenario_row["status"]}
        report=connection.execute("SELECT * FROM operation_reports WHERE operation_id=? ORDER BY reported_at DESC LIMIT 1",(operation_id,)).fetchone()
        observations=[dict(item) for item in connection.execute("SELECT * FROM station_observations WHERE operation_id=? ORDER BY recorded_at",(operation_id,)).fetchall()]
        report_snapshot={"report":dict(report) if report else None,"observations":[{**item} for item in observations],"evidence":[dict(x) for x in connection.execute("SELECT * FROM operation_evidence WHERE operation_id=?",(operation_id,)).fetchall()],"operation_status":operation["status"]}
        comparison=build_projection_comparison(scenario,observations,operation["model_version"])
        rec_id=f"REC-{secrets.token_hex(5).upper()}";now=datetime.now(timezone.utc).isoformat();revision=station_revision(connection,session["station"])
        connection.execute("INSERT INTO reconciliations (id,station_id,scenario_id,operation_id,created_at,status,projection_snapshot,observation_snapshot,comparison_result,current_revision,model_version) VALUES (?,?,?,?,?,'NOT_REVIEWED',?,?,?,?,?)",(rec_id,session["station"],operation["scenario_id"],operation_id,now,json.dumps(scenario,sort_keys=True),json.dumps(report_snapshot,sort_keys=True),json.dumps(comparison,sort_keys=True),revision,operation["model_version"]))
        connection.execute("INSERT INTO reconciliation_events (reconciliation_id,event_type,actor_id,actor_role,occurred_at,payload_json) VALUES (?,?,?,?,?,?)",(rec_id,"RECONCILIATION_CREATED",session["username"],session["role"],now,json.dumps({"current_revision":revision,"model_version":operation["model_version"],"projection_snapshot":scenario,"observation_snapshot":report_snapshot,"comparison_result":comparison},sort_keys=True)))
        return reconciliation_document(connection,connection.execute("SELECT * FROM reconciliations WHERE id=?",(rec_id,)).fetchone())


@app.get("/api/reconciliation/{reconciliation_id}")
def get_reconciliation(reconciliation_id: str, authorization: str | None = Header(default=None)) -> dict:
    session=authorized_session(authorization,allowed_roles={"hq"})
    with database() as connection:
        row=connection.execute("SELECT * FROM reconciliations WHERE id=? AND station_id=?",(reconciliation_id,session["station"])).fetchone()
        if row is None: raise HTTPException(status_code=404,detail="Reconciliation not found")
        return reconciliation_document(connection,row)


@app.post("/api/reconciliation/{reconciliation_id}/review")
def review_reconciliation(reconciliation_id: str, payload: ReconciliationReviewInput, authorization: str | None = Header(default=None)) -> dict:
    session=authorized_session(authorization,allowed_roles={"hq"})
    with database() as connection:
        row=connection.execute("SELECT * FROM reconciliations WHERE id=? AND station_id=?",(reconciliation_id,session["station"])).fetchone()
        if row is None: raise HTTPException(status_code=404,detail="Reconciliation not found")
        if row["status"] not in {"NOT_REVIEWED","REOPENED"}: raise HTTPException(status_code=409,detail="Reconciliation is already reviewed; reopen it before a new review")
        op=connection.execute("SELECT * FROM remote_operations WHERE id=?",(row["operation_id"],)).fetchone()
        event_id=row["operation_id"]
    old={"CONSISTENT_WITH_PROJECTION":"CONSISTENT","PARTIALLY_CONSISTENT":"PARTIALLY_CONSISTENT","DIFFERS_FROM_PROJECTION":"NOT_CONSISTENT","INSUFFICIENT_EVIDENCE":"INSUFFICIENT_EVIDENCE","NOT_COMPARABLE":"INSUFFICIENT_EVIDENCE"}[payload.classification]
    if op["status"] in {"REPORTED_COMPLETE","REPORTED_BLOCKED"}:
        transition_operation(event_id,authorization,{"hq"},{op["status"]},"REVIEWED_OUTCOME","OUTCOME_REVIEWED",{"classification":old,"reconciliation_id":reconciliation_id,"note":payload.notes,"verification":"HUMAN_REVIEW_OF_STATION_REPORT"})
    elif op["status"]=="REOPENED":
        transition_operation(event_id,authorization,{"hq"},{"REOPENED"},"REVIEWED_OUTCOME","OUTCOME_REVIEWED",{"classification":old,"reconciliation_id":reconciliation_id,"note":payload.notes,"verification":"HUMAN_REVIEW_OF_STATION_REPORT"})
    now=datetime.now(timezone.utc).isoformat()
    with database() as connection:
        connection.execute("UPDATE reconciliations SET status='REVIEWED',reviewed_at=?,reviewed_by=?,review_classification=?,review_notes=?,current_revision=? WHERE id=?",(now,session["username"],payload.classification,payload.notes,station_revision(connection,session["station"]),reconciliation_id))
        connection.execute("UPDATE operation_reports SET review_status='HQ_REVIEWED' WHERE operation_id=? AND review_status='PENDING'",(event_id,))
        connection.execute("INSERT INTO operation_reviews (operation_id,reviewed_by,reviewed_at,classification,notes) VALUES (?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET reviewed_by=excluded.reviewed_by,reviewed_at=excluded.reviewed_at,classification=excluded.classification,notes=excluded.notes",(event_id,session["username"],now,old,payload.notes))
        connection.execute("INSERT INTO reconciliation_events (reconciliation_id,event_type,actor_id,actor_role,occurred_at,payload_json) VALUES (?,?,?,?,?,?)",(reconciliation_id,"RECONCILIATION_REVIEWED",session["username"],session["role"],now,json.dumps({"classification":payload.classification,"notes":payload.notes,"follow_up_action":payload.follow_up_action},sort_keys=True)))
        return reconciliation_document(connection,connection.execute("SELECT * FROM reconciliations WHERE id=?",(reconciliation_id,)).fetchone())


@app.post("/api/reconciliation/{reconciliation_id}/reopen")
def reopen_reconciliation(reconciliation_id: str, payload: OperationActionInput, authorization: str | None = Header(default=None)) -> dict:
    session=authorized_session(authorization,allowed_roles={"hq"})
    with database() as connection:
        row=connection.execute("SELECT * FROM reconciliations WHERE id=? AND station_id=?",(reconciliation_id,session["station"])).fetchone()
        if row is None: raise HTTPException(status_code=404,detail="Reconciliation not found")
        if row["status"]!="REVIEWED": raise HTTPException(status_code=409,detail="Only a reviewed reconciliation can be reopened")
        op=connection.execute("SELECT * FROM remote_operations WHERE id=?",(row["operation_id"],)).fetchone()
        projection_snapshot=json.loads(row["projection_snapshot"])
        old_observation_snapshot=json.loads(row["observation_snapshot"])
    if op["status"] in {"CLOSED","REVIEWED_OUTCOME"}:
        transition_operation(row["operation_id"],authorization,{"hq"},{op["status"]},"REOPENED","OPERATION_REOPENED",{"reconciliation_id":reconciliation_id,"note":payload.note})
    now=datetime.now(timezone.utc).isoformat()
    with database() as connection:
        report=connection.execute("SELECT * FROM operation_reports WHERE operation_id=? ORDER BY reported_at DESC LIMIT 1",(row["operation_id"],)).fetchone()
        observations=[dict(x) for x in connection.execute("SELECT * FROM station_observations WHERE operation_id=? ORDER BY recorded_at",(row["operation_id"],)).fetchall()]
        evidence=[dict(x) for x in connection.execute("SELECT * FROM operation_evidence WHERE operation_id=? ORDER BY added_at",(row["operation_id"],)).fetchall()]
        observation_snapshot={"report":dict(report) if report else None,"observations":observations,"evidence":evidence,"operation_status":"REOPENED"}
        comparison=build_projection_comparison(projection_snapshot,observations,row["model_version"])
        current=station_revision(connection,session["station"])
        connection.execute("UPDATE reconciliations SET status='REOPENED',reviewed_at=NULL,reviewed_by=NULL,review_classification=NULL,review_notes='',observation_snapshot=?,comparison_result=?,current_revision=? WHERE id=?",(json.dumps(observation_snapshot,sort_keys=True),json.dumps(comparison,sort_keys=True),current,reconciliation_id))
        connection.execute("INSERT INTO reconciliation_events (reconciliation_id,event_type,actor_id,actor_role,occurred_at,payload_json) VALUES (?,?,?,?,?,?)",(reconciliation_id,"RECONCILIATION_REOPENED",session["username"],session["role"],now,json.dumps({"prior_classification":row["review_classification"],"prior_notes":row["review_notes"],"note":payload.note,"new_observation_snapshot":observation_snapshot,"new_comparison_result":comparison},sort_keys=True)))
        return reconciliation_document(connection,connection.execute("SELECT * FROM reconciliations WHERE id=?",(reconciliation_id,)).fetchone())


@app.get("/api/reconciliation/{reconciliation_id}/provenance")
def reconciliation_provenance(reconciliation_id: str, authorization: str | None = Header(default=None)) -> dict:
    session=authorized_session(authorization,allowed_roles={"hq"})
    with database() as connection:
        row=connection.execute("SELECT * FROM reconciliations WHERE id=? AND station_id=?",(reconciliation_id,session["station"])).fetchone()
        if row is None: raise HTTPException(status_code=404,detail="Reconciliation not found")
        snapshot=json.loads(row["observation_snapshot"])
        sync=connection.execute("SELECT event_id,event_type,server_revision,sync_status FROM sync_event_log WHERE operation_id=? AND station_id=? ORDER BY created_at",(row["operation_id"],session["station"])).fetchall()
        report=snapshot.get("report")
        return {"chain":[{"type":"SCENARIO","id":row["scenario_id"]},{"type":"OPERATION","id":row["operation_id"]},{"type":"OBSERVATION","id":snapshot.get("observations",[{}])[0].get("id") if snapshot.get("observations") else None},{"type":"REPORT","id":report.get("id") if report else None},{"type":"SYNC_EVENTS","items":[dict(x) for x in sync]},{"type":"RECONCILIATION","id":row["id"]},{"type":"HQ_REVIEW","reviewed_by":row["reviewed_by"],"reviewed_at":row["reviewed_at"]}]}


@app.post("/api/reconciliation/{reconciliation_id}/follow-up")
def create_reconciliation_follow_up(reconciliation_id: str, payload: ReconciliationFollowUpInput, authorization: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
    session=authorized_session(authorization,allowed_roles={"hq"})
    if not idempotency_key: raise HTTPException(status_code=400,detail="Idempotency-Key is required")
    with database() as connection:
        rec=connection.execute("SELECT * FROM reconciliations WHERE id=? AND station_id=?",(reconciliation_id,session["station"])).fetchone()
        if rec is None: raise HTTPException(status_code=404,detail="Reconciliation not found")
        if rec["status"]!="REVIEWED": raise HTTPException(status_code=409,detail="Review the reconciliation before creating a follow-up")
        current=station_revision(connection,session["station"])
        if rec["follow_up_operation_id"]:
            row=connection.execute("SELECT * FROM remote_operations WHERE id=?",(rec["follow_up_operation_id"],)).fetchone()
            return operation_document(connection,row)
        operation_id=f"FU-{secrets.token_hex(5).upper()}";now=datetime.now(timezone.utc).isoformat()
        scenario_id=rec["scenario_id"]
        scenario=connection.execute("SELECT * FROM scenarios WHERE id=? AND station=?",(scenario_id,session["station"])).fetchone() if scenario_id else None
        if payload.operation_type=="ACTION_PLAN" and not scenario: raise HTTPException(status_code=422,detail="Action plan follow-ups require the original linked scenario")
        metadata={"original_operation_id":rec["operation_id"],"original_scenario_id":scenario_id,"original_reconciliation_id":reconciliation_id,"current_station_revision":current}
        if payload.operation_type=="MEASUREMENT_REQUEST":
            if not payload.metric or not payload.unit: raise HTTPException(status_code=422,detail="Measurement follow-ups require a metric and unit")
            metadata.update({"metric":payload.metric,"unit":payload.unit})
        elif payload.operation_type=="INSPECTION_REQUEST":
            if not payload.asset or not payload.requested_checks: raise HTTPException(status_code=422,detail="Inspection follow-ups require an asset and requested checks")
            metadata.update({"asset":payload.asset,"requested_checks":payload.requested_checks})
        elif payload.operation_type=="ACTION_PLAN":
            if not payload.candidate_action: raise HTTPException(status_code=422,detail="Action plan follow-ups require a candidate action")
            metadata["candidate_action"]=payload.candidate_action
        provenance={"source_type":"HQ_REVIEW","reconciliation_id":reconciliation_id,"scenario_id":scenario_id}
        row,created=create_operation_row(connection,session,operation_id,payload.operation_type,payload.title,payload.description,payload.priority,"station",current,scenario_id,scenario["model_version"] if scenario else None,None,None,provenance,metadata,idempotency_key)
        if created and payload.operation_type=="MEASUREMENT_REQUEST":
            connection.execute("INSERT INTO measurement_requests (id,station,metric,unit,reason,deadline,related_scenario,base_revision,status,created_at,created_by,scenario_id,model_version) VALUES (?,?,?,?,?,?,?,?, 'DRAFT', ?, ?, ?, ?)",(operation_id,session["station"],payload.metric,payload.unit,payload.description,"",scenario_id,current,now,session["username"],scenario_id,scenario["model_version"] if scenario else None))
        elif created and payload.operation_type=="INSPECTION_REQUEST":
            connection.execute("INSERT INTO inspection_requests (id,station,asset,requested_checks,reason,deadline,base_revision,status,created_at,created_by,scenario_id,model_version) VALUES (?,?,?,?,?,?,?,'DRAFT',?,?,?,?)",(operation_id,session["station"],payload.asset,payload.requested_checks,payload.description,"",current,now,session["username"],scenario_id,scenario["model_version"] if scenario else None))
        elif created and payload.operation_type=="ACTION_PLAN":
            connection.execute("INSERT INTO remote_operation_proposals (id,station,scenario_id,title,base_revision,model_version,status,created_at,created_by) VALUES (?,?,?,?,?,?,'DRAFT',?,?)",(operation_id,session["station"],scenario_id,payload.title,current,scenario["model_version"],now,session["username"]))
        connection.execute("UPDATE reconciliations SET follow_up_operation_id=? WHERE id=?",(operation_id,reconciliation_id))
        connection.execute("INSERT INTO reconciliation_events (reconciliation_id,event_type,actor_id,actor_role,occurred_at,payload_json) VALUES (?,?,?,?,?,?)",(reconciliation_id,"FOLLOW_UP_OPERATION_CREATED",session["username"],session["role"],now,json.dumps({"operation_id":operation_id,"current_revision":current,"idempotency_key":idempotency_key},sort_keys=True)))
        return operation_document(connection,row)


@app.post("/api/operations/{operation_id}/reopen")
def reopen_operation(operation_id: str, payload: OperationActionInput, authorization: str | None = Header(default=None)) -> dict:
    return transition_operation(operation_id, authorization, {"hq"}, {"CLOSED", "REVIEWED_OUTCOME"}, "REOPENED", "OPERATION_REOPENED", {"note": payload.note})


@app.post("/api/operations/{operation_id}/close")
def close_operation(operation_id: str, payload: OperationActionInput, authorization: str | None = Header(default=None)) -> dict:
    return transition_operation(operation_id, authorization, {"hq"}, {"REVIEWED_OUTCOME"}, "CLOSED", "OPERATION_CLOSED", {"note": payload.note})


@app.post("/api/operations/{operation_id}/evidence")
def add_operation_evidence(operation_id: str, payload: OperationEvidenceInput, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"station", "technical"})
    evidence_id = f"EV-{secrets.token_hex(5).upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with database() as connection:
        row = operation_for_session(connection, operation_id, session)
        if session["role"] == "technical" and row["assigned_role"] not in {"technical", "both"}:
            raise HTTPException(status_code=403, detail="Technical role may only add evidence to assigned operations")
        connection.execute("INSERT INTO operation_evidence (id, operation_id, source_type, label, uri, digest, added_by, added_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (evidence_id, operation_id, payload.source_type, payload.label, payload.uri, payload.digest, session["username"], now, json.dumps(payload.metadata, sort_keys=True)))
        append_operation_event(connection, row, session, "EVIDENCE_ATTACHED", row["status"], row["status"], {"evidence_id": evidence_id, "source_type": payload.source_type})
    return {"id": evidence_id, "operation_id": operation_id, **payload.model_dump(), "added_by": session["username"], "added_at": now}


@app.post("/api/operations/{operation_id}/technical-observation")
def add_technical_observation(operation_id: str, payload: OperationReportInput, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"technical"})
    with database() as connection:
        row = operation_for_session(connection, operation_id, session)
        now = datetime.now(timezone.utc).isoformat()
        report_id = f"TO-{secrets.token_hex(5).upper()}"
        connection.execute("INSERT INTO operation_reports (id, operation_id, report_type, reported_by, reported_role, reported_at, station_revision, observations_json, evidence_ids_json, review_status) VALUES (?, ?, 'TECHNICAL_OBSERVATION', ?, ?, ?, ?, ?, ?, 'PENDING')", (report_id, operation_id, session["username"], session["role"], now, station_revision(connection, row["station_id"]), json.dumps(payload.observations, sort_keys=True), json.dumps(payload.evidence_ids)))
        append_operation_event(connection, row, session, "TECHNICAL_OBSERVATION_ADDED", row["status"], row["status"], {"report_id": report_id, "note": payload.note, "verification": "TECHNICAL_ROLE_OBSERVATION"})
        updated = connection.execute("SELECT * FROM remote_operations WHERE id = ?", (operation_id,)).fetchone()
        return operation_document(connection, updated)


@app.get("/api/technical/context/{operation_id}")
def technical_operation_context(operation_id: str, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"technical"})
    with database() as connection:
        operation = operation_for_session(connection, operation_id, session)
        if operation["assigned_role"] not in {"technical", "both"}:
            raise HTTPException(status_code=403, detail="Technical role may only inspect assigned operations")
        scenario = None
        if operation["scenario_id"]:
            row = connection.execute("SELECT * FROM scenarios WHERE id=? AND station=?", (operation["scenario_id"], session["station"])).fetchone()
            if row:
                scenario = {"id": row["id"], "name": row["name"], "station_id": row["station"],
                    "base_revision": row["base_revision"], "model_version": row["model_version"],
                    "created_at": row["created_at"], "status": row["status"],
                    "inputs": json.loads(row["inputs_json"]), "baseline": json.loads(row["baseline_json"]),
                    "result": json.loads(row["result_json"])}
        state = [{"field": row["field"], "value": json.loads(row["value_json"]), "unit": row["unit"],
            "source_type": row["source_kind"], "observed_at": row["timestamp"], "quality": row["quality"],
            "revision": row["revision"], "model_version": row["model_version"]}
            for row in connection.execute("SELECT * FROM station_state WHERE station=? ORDER BY field", (session["station"],)).fetchall()]
        revision = station_revision(connection, session["station"])
        weather = _latest_official_weather(connection, session["station"])
        findings = [technical_finding_document(connection, row) for row in connection.execute(
            "SELECT * FROM technical_findings WHERE operation_id=? ORDER BY created_at", (operation_id,)).fetchall()]
        return {"station_id": session["station"], "current_revision": revision,
            "operation": operation_document(connection, operation), "scenario": scenario, "state": state,
            "weather": weather, "technical_findings": findings}


def technical_finding_document(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    finding = dict(row)
    for column, key in (("observations_json", "observations"), ("evidence_refs_json", "evidence_refs"), ("provenance_json", "provenance")):
        finding[key] = json.loads(finding.pop(column))
    finding["events"] = [{**dict(event), "payload": json.loads(event["payload_json"])} for event in connection.execute(
        "SELECT * FROM technical_finding_events WHERE finding_id=? ORDER BY occurred_at,id", (row["id"],)).fetchall()]
    return finding


def append_technical_finding_event(connection: sqlite3.Connection, finding_id: str, session: sqlite3.Row,
                                  event_type: str, previous_status: str | None, new_status: str,
                                  revision: int, payload: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    event_id = f"TFE-{secrets.token_hex(6).upper()}"
    connection.execute("INSERT INTO technical_finding_events (id,finding_id,event_type,actor_id,actor_role,occurred_at,previous_status,new_status,revision,source,payload_hash,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, finding_id, event_type, session["username"], session["role"], now, previous_status, new_status, revision, "TECHNICAL_ENGINEER", digest, encoded))


DEMO_RUN_ID = "DEMO-PRATIBIMB-001"


def _demo_entities(connection: sqlite3.Connection, records: list[tuple[str, str]]) -> None:
    connection.executemany("INSERT OR IGNORE INTO demo_records (run_id,entity_type,record_id) VALUES (?,?,?)",
                           [(DEMO_RUN_ID, kind, record_id) for kind, record_id in records])


def _demo_run_snapshot(connection: sqlite3.Connection) -> dict:
    rows = connection.execute("SELECT entity_type,record_id FROM demo_records WHERE run_id=? ORDER BY entity_type,record_id", (DEMO_RUN_ID,)).fetchall()
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["entity_type"], []).append(row["record_id"])
    return {"demo_run_id": DEMO_RUN_ID, "station_id": "maitri", "status": "SEEDED" if rows else "ABSENT",
            "records": grouped}


def _reset_demo_run(connection: sqlite3.Connection) -> None:
    entities = connection.execute("SELECT entity_type,record_id FROM demo_records WHERE run_id=?", (DEMO_RUN_ID,)).fetchall()
    ids: dict[str, list[str]] = {}
    for row in entities:
        ids.setdefault(row["entity_type"], []).append(row["record_id"])
    ops = ids.get("OPERATION", [])
    scenarios = ids.get("SCENARIO", [])
    reconciliations = ids.get("RECONCILIATION", [])
    findings = ids.get("TECHNICAL_FINDING", [])
    if ops:
        marks = ",".join("?" for _ in ops)
        event_ids = [r[0] for r in connection.execute(f"SELECT event_id FROM sync_event_log WHERE operation_id IN ({marks})", ops).fetchall()]
        if event_ids:
            event_marks = ",".join("?" for _ in event_ids)
            connection.execute(f"DELETE FROM sync_conflicts WHERE event_id IN ({event_marks})", event_ids)
        for table in ("station_sync_events", "sync_event_log", "operation_events", "operation_reports", "operation_evidence", "station_observations", "operation_reviews"):
            connection.execute(f"DELETE FROM {table} WHERE operation_id IN ({marks})", ops)
        connection.execute(f"DELETE FROM measurement_requests WHERE id IN ({marks})", ops)
        connection.execute(f"DELETE FROM inspection_requests WHERE id IN ({marks})", ops)
        connection.execute(f"DELETE FROM remote_operation_proposals WHERE id IN ({marks})", ops)
    if findings:
        marks = ",".join("?" for _ in findings)
        connection.execute(f"DELETE FROM technical_finding_events WHERE finding_id IN ({marks})", findings)
        connection.execute(f"DELETE FROM technical_findings WHERE id IN ({marks})", findings)
    if reconciliations:
        marks = ",".join("?" for _ in reconciliations)
        connection.execute(f"DELETE FROM reconciliation_events WHERE reconciliation_id IN ({marks})", reconciliations)
        connection.execute(f"DELETE FROM reconciliations WHERE id IN ({marks})", reconciliations)
    if ops:
        marks = ",".join("?" for _ in ops)
        connection.execute(f"DELETE FROM remote_operations WHERE id IN ({marks})", ops)
    if scenarios:
        marks = ",".join("?" for _ in scenarios)
        connection.execute(f"DELETE FROM remote_operation_proposals WHERE scenario_id IN ({marks})", scenarios)
        connection.execute(f"DELETE FROM scenario_results WHERE scenario_id IN ({marks})", scenarios)
        connection.execute(f"DELETE FROM assumptions WHERE scenario_id IN ({marks})", scenarios)
        connection.execute(f"DELETE FROM scenarios WHERE id IN ({marks})", scenarios)
    connection.execute("DELETE FROM demo_records WHERE run_id=?", (DEMO_RUN_ID,))
    connection.execute("DELETE FROM demo_runs WHERE run_id=?", (DEMO_RUN_ID,))


@app.get("/api/demo/status")
def demo_status(authorization: str | None = Header(default=None)) -> dict:
    session = authorized_hq(authorization, "maitri")
    if session["username"] != "admin_hq":
        raise HTTPException(status_code=403, detail="Demo controls are restricted to the local demo administrator")
    with database() as connection:
        return _demo_run_snapshot(connection)


@app.post("/api/demo/reset")
def demo_reset(authorization: str | None = Header(default=None)) -> dict:
    session = authorized_hq(authorization, "maitri")
    if session["username"] != "admin_hq":
        raise HTTPException(status_code=403, detail="Demo controls are restricted to the local demo administrator")
    with database() as connection:
        _reset_demo_run(connection)
    return {"demo_run_id": DEMO_RUN_ID, "status": "ABSENT", "preserved": ["non-demo operations and audit", "NCPOR observations", "historical datasets", "sessions", "station configuration"]}


@app.post("/api/demo/seed")
def demo_seed(authorization: str | None = Header(default=None)) -> dict:
    session = authorized_hq(authorization, "maitri")
    if session["username"] != "admin_hq":
        raise HTTPException(status_code=403, detail="Demo controls are restricted to the local demo administrator")
    with database() as connection:
        current = _demo_run_snapshot(connection)
        if current["status"] == "SEEDED":
            return current
        if connection.execute("SELECT 1 FROM scenarios WHERE id='SC-DEMO-001'").fetchone() or connection.execute("SELECT 1 FROM remote_operations WHERE id='OP-DEMO-001'").fetchone():
            raise HTTPException(status_code=409, detail="A non-demo record uses a reserved deterministic demo identifier; demo data was not changed")
        now = datetime.now(timezone.utc).isoformat()
        run_id = DEMO_RUN_ID
        revision = station_revision(connection, "maitri")
        baseline, _ = scenario_baseline(connection, "maitri", revision)
        parameters = {"chp_available": False, "fuel_consumption_lph": 9.5, "critical_load_kw": 100.0,
            "deferrable_load_kw": 20.0, "outdoor_temperature_c": -24.0, "water_demand_l_per_day": 1000.0, "duration_hours": 24.0}
        result = run_scenario(baseline, parameters)
        scenario_id = "SC-DEMO-001"
        connection.execute("INSERT INTO demo_runs VALUES (?,?,?,?,?)", (run_id, "maitri", "SEEDED", now, session["username"]))
        connection.execute("INSERT INTO scenarios VALUES (?,?,?,?,?,?,?,?,?,'COMPLETED')", (scenario_id, "maitri", "CHP Unavailable · DEMO", revision, MODEL_VERSION, now,
            json.dumps({"parameters": parameters, "weather_reference": None, "demo_run_id": run_id}, sort_keys=True), json.dumps(baseline, sort_keys=True), json.dumps(result, sort_keys=True)))
        connection.execute("INSERT INTO scenario_results VALUES (?,?,?,?,?,?,?)", (scenario_id, "maitri", revision, MODEL_VERSION,
            json.dumps(result["projected"], sort_keys=True), json.dumps(result["constraints"], sort_keys=True), now))
        for assumption in result["assumptions"]:
            connection.execute("INSERT INTO assumptions (scenario_id,name,value_json,unit,assumption_type) VALUES (?,?,?,?,?)",
                (scenario_id, assumption["name"], json.dumps(assumption["value"]), assumption["unit"], assumption["type"]))
        operation_id = "OP-DEMO-001"
        op, _ = create_operation_row(connection, session, operation_id, "INSPECTION_REQUEST", "Thermal plant inspection · DEMO",
            "DEMO · Inspect CHP availability and record a station-reported observation.", "P1", "both", revision, scenario_id, MODEL_VERSION, None, None,
            {"source_type": "DEMO_SYNTHETIC", "demo_run_id": run_id, "scenario_id": scenario_id},
            {"asset": "CHP", "requested_checks": "Availability and visible operating condition", "demo_run_id": run_id}, run_id)
        connection.execute("INSERT INTO inspection_requests (id,station,asset,requested_checks,reason,deadline,base_revision,status,created_at,created_by,scenario_id,model_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (operation_id, "maitri", "CHP", "Availability and visible operating condition", "DEMO · Resolve scenario uncertainty through inspection.", "", revision, "CLOSED", now, session["username"], scenario_id, MODEL_VERSION))
        records: list[tuple[str, str]] = [("SCENARIO", scenario_id), ("SCENARIO_RESULT", scenario_id), ("OPERATION", operation_id)]
        status = "DRAFT"
        timeline = [("REVIEWED_FOR_COMMUNICATION", "hq", "REVIEWED_FOR_COMMUNICATION", {"demo_run_id": run_id}),
            ("QUEUED", "hq", "OPERATION_QUEUED", {"demo_run_id": run_id}), ("DELIVERED", "transport", "OPERATION_DELIVERED", {"demo_run_id": run_id, "source_type": "DEMO_SYNTHETIC"}),
            ("SEEN", "station", "OPERATION_SEEN", {"demo_run_id": run_id}), ("ACCEPTED", "station", "OPERATION_ACCEPTED", {"demo_run_id": run_id}),
            ("IN_PROGRESS", "station", "STATION_REPORTED_IN_PROGRESS", {"demo_run_id": run_id})]
        for next_status, actor, event_type, details in timeline:
            pseudo = {"username": session["username"] if actor == "hq" else "admin_station" if actor == "station" else "demo-transport", "role": "hq" if actor == "hq" else "station" if actor == "station" else "transport"}
            connection.execute("UPDATE remote_operations SET status=?,updated_at=? WHERE id=?", (next_status, now, operation_id))
            op = connection.execute("SELECT * FROM remote_operations WHERE id=?", (operation_id,)).fetchone()
            append_operation_event(connection, op, pseudo, event_type, status, next_status, details, "DEMO_SYNTHETIC" if actor == "transport" else "DEMO")
            status = next_status
        observations = [("OBS-DEMO-001", "CHP Availability", "Available", "state", "2026-10-04T09:30:00+05:30", "Manual visual inspection"),
            ("OBS-DEMO-002", "Indoor Temperature", "-4.2", "°C", "2026-10-04T09:32:00+05:30", "Manual station reading")]
        for obs_id, metric, value, unit, observed_at, method in observations:
            connection.execute("INSERT INTO station_observations VALUES (?,?,?,?,?,?,?,?,?,'MANUAL','UNASSESSED','MEDIUM',?,?,?)",
                (obs_id, "maitri", operation_id, metric, value, unit, observed_at, now, method, "DEMO · entered by station role", "admin_station", revision))
            records.append(("OBSERVATION", obs_id))
            op = connection.execute("SELECT * FROM remote_operations WHERE id=?", (operation_id,)).fetchone()
            append_operation_event(connection, op, {"username": "admin_station", "role": "station"}, "STATION_OBSERVATION_RECORDED", status, status,
                {"observation_id": obs_id, "metric": metric, "source_type": "MANUAL", "demo_run_id": run_id}, "DEMO")
        report_id = "RPT-DEMO-001"
        report_body = {"summary": "DEMO · CHP reported available after manual inspection", "observations": ["OBS-DEMO-001", "OBS-DEMO-002"], "source_type": "MANUAL"}
        connection.execute("UPDATE remote_operations SET status='REPORTED_COMPLETE',updated_at=? WHERE id=?", (now, operation_id))
        op = connection.execute("SELECT * FROM remote_operations WHERE id=?", (operation_id,)).fetchone()
        append_operation_event(connection, op, {"username": "admin_station", "role": "station"}, "REPORT_COMPLETE", status, "REPORTED_COMPLETE", {"demo_run_id": run_id, "source_type": "MANUAL"}, "DEMO")
        status = "REPORTED_COMPLETE"
        connection.execute("INSERT INTO operation_reports VALUES (?,?,?,?,?,?,?,?,?,'HQ_REVIEWED')", (report_id, operation_id, "REPORTED_COMPLETE", "admin_station", "station", now, revision, json.dumps(report_body, sort_keys=True), "[]"))
        records.append(("REPORT", report_id))
        sync_id = "SYNC-DEMO-001"
        sync_body = json.dumps({"demo_run_id": run_id, "action": "report-complete", "operation_id": operation_id}, sort_keys=True)
        sync_hash = hashlib.sha256(sync_body.encode("utf-8")).hexdigest()
        connection.execute("INSERT INTO sync_event_log (event_id,idempotency_key,device_id,station_id,operation_id,event_type,created_at,local_revision,payload_hash,payload_json,sync_status,attempt_count,last_attempt_at,server_event_id,server_revision) VALUES (?,?,?,?,?,?,?,?,?,?,'SYNCED',1,?,?,?)",
            (sync_id, "DEMO-IDEM-001", "DEMO-MAITRI-DEVICE", "maitri", operation_id, "report-complete", now, revision, sync_hash, sync_body, now, "DEMO-SERVER-EVENT-001", revision))
        connection.execute("INSERT INTO station_sync_events VALUES (?,?,?,?,?,?)", ("DEMO-IDEM-001", "maitri", operation_id, "report-complete", json.dumps({"demo_run_id": run_id, "sync_state": "SYNCED"}), now))
        connection.execute("UPDATE remote_operations SET sync_state='SYNCED' WHERE id=?", (operation_id,))
        op = connection.execute("SELECT * FROM remote_operations WHERE id=?", (operation_id,)).fetchone()
        append_operation_event(connection, op, {"username": "demo-transport", "role": "transport"}, "REPORT_SYNCHRONIZED", status, status, {"demo_run_id": run_id, "sync_event_id": sync_id}, "DEMO_SYNTHETIC")
        connection.execute("UPDATE remote_operations SET status='REVIEWED_OUTCOME',updated_at=? WHERE id=?", (now, operation_id))
        op = connection.execute("SELECT * FROM remote_operations WHERE id=?", (operation_id,)).fetchone()
        append_operation_event(connection, op, session, "OUTCOME_REVIEWED", status, "REVIEWED_OUTCOME", {"demo_run_id": run_id, "classification": "INSUFFICIENT_EVIDENCE"}, "DEMO")
        status = "REVIEWED_OUTCOME"
        connection.execute("UPDATE remote_operations SET status='CLOSED',updated_at=? WHERE id=?", (now, operation_id))
        op = connection.execute("SELECT * FROM remote_operations WHERE id=?", (operation_id,)).fetchone()
        append_operation_event(connection, op, session, "OPERATION_CLOSED", status, "CLOSED", {"demo_run_id": run_id}, "DEMO")
        records.append(("SYNC_EVENT", sync_id))
        operation = connection.execute("SELECT * FROM remote_operations WHERE id=?", (operation_id,)).fetchone()
        scenario = {"id": scenario_id, "station": "maitri", "name": "CHP Unavailable · DEMO", "base_revision": revision, "model_version": MODEL_VERSION,
            "created_at": now, "inputs": parameters, "weather_reference": None, "baseline": baseline, "result": result, "status": "COMPLETED"}
        observation_rows = [dict(row) for row in connection.execute("SELECT * FROM station_observations WHERE operation_id=?", (operation_id,)).fetchall()]
        report = dict(connection.execute("SELECT * FROM operation_reports WHERE id=?", (report_id,)).fetchone())
        report_snapshot = {"report": report, "observations": observation_rows, "evidence": [], "operation_status": "CLOSED"}
        comparison = build_projection_comparison(scenario, observation_rows)
        reconciliation_id = "REC-DEMO-001"
        review_notes = "DEMO · Human review records the difference between scenario assumption and station-reported observation; no automatic correctness claim."
        connection.execute("INSERT INTO reconciliations (id,station_id,scenario_id,operation_id,created_at,reviewed_at,reviewed_by,status,projection_snapshot,observation_snapshot,comparison_result,review_classification,review_notes,current_revision,model_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (reconciliation_id, "maitri", scenario_id, operation_id, now, now, session["username"], "REVIEWED", json.dumps(scenario, sort_keys=True), json.dumps(report_snapshot, sort_keys=True), json.dumps(comparison, sort_keys=True), "DIFFERS_FROM_PROJECTION", review_notes, revision, MODEL_VERSION))
        connection.execute("INSERT INTO reconciliation_events (reconciliation_id,event_type,actor_id,actor_role,occurred_at,payload_json) VALUES (?,?,?,?,?,?)",
            (reconciliation_id, "RECONCILIATION_CREATED", session["username"], "hq", now, json.dumps({"demo_run_id": run_id, "comparison_result": comparison}, sort_keys=True)))
        connection.execute("INSERT INTO reconciliation_events (reconciliation_id,event_type,actor_id,actor_role,occurred_at,payload_json) VALUES (?,?,?,?,?,?)",
            (reconciliation_id, "RECONCILIATION_REVIEWED", session["username"], "hq", now, json.dumps({"demo_run_id": run_id, "classification": "DIFFERS_FROM_PROJECTION", "notes": review_notes}, sort_keys=True)))
        connection.execute("INSERT INTO operation_reviews VALUES (?,?,?,?,?)", (operation_id, session["username"], now, "NOT_CONSISTENT", review_notes))
        records.append(("RECONCILIATION", reconciliation_id))
        evidence_id = "EV-DEMO-001"
        connection.execute("INSERT INTO operation_evidence VALUES (?,?,?,?,?,?,?,?,?)", (evidence_id, operation_id, "MANUAL", "DEMO · visual inspection note", None, None, "admin_tech", now, json.dumps({"demo_run_id": run_id, "recorded_source": "TECHNICAL_ENGINEER"}, sort_keys=True)))
        finding_id = "TF-DEMO-001"
        finding_data = {"condition": "CHP reported available", "source": "MANUAL", "demo_run_id": run_id}
        connection.execute("INSERT INTO technical_findings VALUES (?,?,?,?,?,?,'COMPLETE',?,?,?,?,?,?,?,?,?)",
            (finding_id, operation_id, "maitri", "admin_tech", now, now, "Manual visual inspection recorded; no visible issue reported.", json.dumps(finding_data, sort_keys=True), "No load test performed.", "Confirm during the next scheduled plant check.", "", json.dumps([evidence_id]), json.dumps({"source_type": "MANUAL", "demo_run_id": run_id}, sort_keys=True), revision, run_id))
        for event_id, event_type, prev_status, next_status, event_payload in (
            ("TFE-DEMO-001-OPEN", "TECHNICAL_FINDING_CREATED", None, "OPEN", {"demo_run_id": run_id}),
            ("TFE-DEMO-001-COMPLETE", "TECHNICAL_FINDING_COMPLETE", "OPEN", "COMPLETE", {"summary": "Visual inspection recorded.", "demo_run_id": run_id})):
            encoded = json.dumps(event_payload, sort_keys=True, separators=(",", ":"))
            connection.execute("INSERT INTO technical_finding_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, finding_id, event_type, "admin_tech", "technical", now, prev_status, next_status, revision, "DEMO", hashlib.sha256(encoded.encode()).hexdigest(), encoded))
        records.extend([("EVIDENCE", evidence_id), ("TECHNICAL_FINDING", finding_id)])
        _demo_entities(connection, records)
        return _demo_run_snapshot(connection)


@app.post("/api/technical/findings/{operation_id}")
def create_technical_finding(operation_id: str, payload: TechnicalFindingInput, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"technical"})
    finding_id = f"TF-{secrets.token_hex(6).upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with database() as connection:
        operation = operation_for_session(connection, operation_id, session)
        if operation["assigned_role"] not in {"technical", "both"}:
            raise HTTPException(status_code=403, detail="Technical role may only report on assigned operations")
        if payload.evidence_refs:
            marks = ",".join("?" for _ in payload.evidence_refs)
            evidence = connection.execute(f"SELECT id FROM operation_evidence WHERE operation_id=? AND id IN ({marks})", (operation_id, *payload.evidence_refs)).fetchall()
            if len(evidence) != len(set(payload.evidence_refs)):
                raise HTTPException(status_code=422, detail="Evidence reference is missing or belongs to another operation")
        run = connection.execute("SELECT run_id FROM demo_records WHERE entity_type='OPERATION' AND record_id=? ORDER BY run_id LIMIT 1", (operation_id,)).fetchone()
        run_id = run["run_id"] if run else None
        revision = station_revision(connection, session["station"])
        connection.execute("INSERT INTO technical_findings (id,operation_id,station_id,technical_user_id,created_at,updated_at,status,finding,observations_json,constraints_text,recommendation,blocked_reason,evidence_refs_json,provenance_json,revision,demo_run_id) VALUES (?,?,?,?,?,?,'OPEN',?,?,?,?,?,?,?, ?,?)",
            (finding_id, operation_id, session["station"], session["username"], now, now, payload.finding.strip(), json.dumps(payload.observations, sort_keys=True), payload.constraints, payload.recommendation, "", json.dumps(payload.evidence_refs), json.dumps({"source_type": "MANUAL", "operation_id": operation_id, "station_id": session["station"]}, sort_keys=True), revision, run_id))
        if run_id:
            connection.execute("INSERT OR IGNORE INTO demo_records (run_id,entity_type,record_id) VALUES (?,'TECHNICAL_FINDING',?)", (run_id, finding_id))
        append_technical_finding_event(connection, finding_id, session, "TECHNICAL_FINDING_CREATED", None, "OPEN", revision, {"operation_id": operation_id, "evidence_refs": payload.evidence_refs})
        return technical_finding_document(connection, connection.execute("SELECT * FROM technical_findings WHERE id=?", (finding_id,)).fetchone())


@app.post("/api/technical/findings/{finding_id}/{disposition}")
def dispose_technical_finding(finding_id: str, disposition: str, payload: TechnicalFindingDispositionInput,
                              authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization, allowed_roles={"technical"})
    if disposition not in {"complete", "blocked"}:
        raise HTTPException(status_code=404, detail="Technical finding action not found")
    status = "COMPLETE" if disposition == "complete" else "BLOCKED"
    if status == "BLOCKED" and len(payload.reason.strip()) < 5:
        raise HTTPException(status_code=422, detail="A clear reason is required to mark a technical finding blocked")
    with database() as connection:
        row = connection.execute("SELECT f.* FROM technical_findings f JOIN remote_operations o ON o.id=f.operation_id WHERE f.id=? AND f.station_id=? AND o.assigned_role IN ('technical','both')", (finding_id, session["station"])).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Technical finding not found for this station")
        if row["technical_user_id"] != session["username"]:
            raise HTTPException(status_code=403, detail="Only the finding author may set its disposition")
        if row["status"] != "OPEN":
            raise HTTPException(status_code=409, detail="Technical finding is already disposed")
        if status == "COMPLETE":
            finding_summary = payload.summary.strip()
            blocked_reason = ""
        else:
            finding_summary = row["finding"]
            blocked_reason = payload.reason.strip()
        revision = station_revision(connection, session["station"])
        connection.execute("UPDATE technical_findings SET status=?,finding=?,blocked_reason=?,updated_at=?,revision=? WHERE id=? AND status='OPEN'", (status, finding_summary, blocked_reason, datetime.now(timezone.utc).isoformat(), revision, finding_id))
        append_technical_finding_event(connection, finding_id, session, f"TECHNICAL_FINDING_{status}", "OPEN", status, revision, {"summary": payload.summary.strip(), "reason": blocked_reason})
        return technical_finding_document(connection, connection.execute("SELECT * FROM technical_findings WHERE id=?", (finding_id,)).fetchone())


@app.get("/stations")
def stations() -> list[dict[str, str]]:
    return STATIONS


@app.get("/roles")
def roles() -> list[dict[str, str]]:
    return ROLES


@app.post("/auth/login", response_model=UserSession)
def login(payload: LoginRequest) -> UserSession:
    expected_role = USERS.get(payload.username)
    if payload.password != "123" or expected_role is None or expected_role != payload.role:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if payload.station not in {station["id"] for station in STATIONS}:
        raise HTTPException(status_code=400, detail="Invalid station")

    token = secrets.token_urlsafe(24)
    created_at = datetime.now(timezone.utc).isoformat()
    with database() as connection:
        connection.execute("INSERT INTO sessions (token, username, role, station, created_at) VALUES (?, ?, ?, ?, ?)", (token, payload.username, payload.role, payload.station, created_at))
    return UserSession(token=token, username=payload.username, role=payload.role, station=payload.station, created_at=created_at)


@app.post("/auth/logout")
def logout(authorization: str | None = Header(default=None)) -> dict:
    session = authorized_session(authorization)
    with database() as connection:
        connection.execute("DELETE FROM sessions WHERE token=?", (session["token"],))
    return {"status": "SIGNED_OUT"}


def station_revision(connection: sqlite3.Connection, station: str) -> int:
    row = connection.execute("SELECT COALESCE(MAX(id), 0) FROM station_events WHERE station = ?", (station,)).fetchone()
    return int(row[0])


def scenario_baseline(connection: sqlite3.Connection, station: str, revision: int) -> tuple[dict, dict | None]:
    rows = connection.execute("SELECT * FROM station_state WHERE station = ?", (station,)).fetchall()
    state = {row['field']: {"value": json.loads(row['value_json']), "unit": row['unit'], "source": row['source_kind'], "source_kind": row['source_kind'], "timestamp": row['timestamp'], "quality": row['quality'], "revision": row['revision'], "model_version": row['model_version'], "evidence_refs": []} for row in rows}
    if not state:
        state = emulator_snapshot(station, revision)
    events = connection.execute("SELECT * FROM station_events WHERE station = ? ORDER BY id ASC", (station,)).fetchall()
    for event in events:
        try:
            payload = json.loads(event["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        source = event["source"]
        source_lower = source.lower()
        kind = "DEMO_SYNTHETIC" if "synthetic" in source_lower or "emulator" in source_lower else "MANUAL_DEMO_ENTRY" if "manual" in source_lower else "UNKNOWN"
        for key, value in payload.items():
            if key in state and value is not None:
                state[key] = {"value": value, "unit": state[key]["unit"], "source": source, "source_kind": kind, "timestamp": event["occurred_at"], "quality": "UNKNOWN", "revision": event["id"], "model_version": event["model_version"], "evidence_refs": json.loads(event["evidence_refs"] or "[]")}
    for field, item in state.items():
        connection.execute("INSERT INTO station_state (station, field, value_json, source_kind, timestamp, quality, revision, model_version, unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(station, field) DO UPDATE SET value_json=excluded.value_json, source_kind=excluded.source_kind, timestamp=excluded.timestamp, quality=excluded.quality, revision=excluded.revision, model_version=excluded.model_version, unit=excluded.unit", (station, field, json.dumps(item.get('value')), item.get('source_kind', 'UNKNOWN'), item.get('timestamp'), item.get('quality', 'UNKNOWN'), item.get('revision', revision), item.get('model_version'), item.get('unit', 'unknown')))
    weather_rows = load_weather(station)
    latest_weather = weather_rows[-1] if weather_rows else None
    if latest_weather and latest_weather.get("temperature_c") is not None:
        state["heat_outdoor"] = {"value": latest_weather["temperature_c"], "unit": "\u00b0C", "source": latest_weather["source"], "source_kind": latest_weather["source_type"], "timestamp": latest_weather["timestamp"], "quality": latest_weather["quality"], "revision": revision, "model_version": None, "evidence_refs": [latest_weather.get("source_url", "") ]}
    return state, latest_weather


@app.get("/hq/scenario/context/{station}")
def hq_scenario_context(station: str, authorization: str | None = Header(default=None)) -> dict:
    authorized_hq(authorization, station)
    with database() as connection:
        revision = station_revision(connection, station)
        baseline, latest_weather = scenario_baseline(connection, station, revision)
        history = connection.execute("SELECT id, name, base_revision, model_version, created_at, status FROM scenarios WHERE station = ? ORDER BY created_at DESC LIMIT 12", (station,)).fetchall()
    historical = load_local_historical(station)
    return {"station_id": station, "base_revision": revision, "model_version": MODEL_VERSION, "baseline": baseline, "weather": latest_weather, "weather_status": "CONFIGURED" if latest_weather else "NOT_CONFIGURED", "historical_weather": historical, "historical_weather_status": "CONFIGURED" if historical else "NOT_CONFIGURED", "history": [dict(row) for row in history]}


@app.get("/hq/scenarios/{station}")
def list_scenarios(station: str, authorization: str | None = Header(default=None)) -> list[dict]:
    authorized_hq(authorization, station)
    with database() as connection:
        rows = connection.execute("SELECT id, station, name, base_revision, model_version, created_at, status FROM scenarios WHERE station = ? ORDER BY created_at DESC LIMIT 30", (station,)).fetchall()
    return [dict(row) for row in rows]


@app.get("/hq/scenarios/{station}/{scenario_id}")
def get_scenario(station: str, scenario_id: str, authorization: str | None = Header(default=None)) -> dict:
    authorized_hq(authorization, station)
    with database() as connection:
        row = connection.execute("SELECT * FROM scenarios WHERE station = ? AND id = ?", (station, scenario_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Scenario not found for this station")
    result = dict(row)
    raw_inputs = json.loads(result.pop("inputs_json"))
    if isinstance(raw_inputs, dict) and "parameters" in raw_inputs:
        result["inputs"] = raw_inputs["parameters"]
        result["weather_reference"] = raw_inputs.get("weather_reference")
    else:
        result["inputs"] = raw_inputs
        result["weather_reference"] = None
    result["baseline"] = json.loads(result.pop("baseline_json"))
    result["result"] = json.loads(result.pop("result_json"))
    return result


@app.post("/hq/scenarios/{station}")
def create_scenario(station: str, payload: ScenarioInput, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_hq(authorization, station)
    if payload.model_version != MODEL_VERSION:
        raise HTTPException(status_code=409, detail=f"Model version changed. Refresh and use {MODEL_VERSION}.")
    now = datetime.now(timezone.utc).isoformat()
    scenario_id = f"SC-{secrets.token_hex(5).upper()}"
    with database() as connection:
        revision = station_revision(connection, station)
        if payload.base_revision != revision:
            raise HTTPException(status_code=409, detail="Station revision changed. Refresh the baseline before running this scenario.")
        baseline, _ = scenario_baseline(connection, station, revision)
        weather_reference = payload.weather_reference
        if weather_reference is not None:
            candidate = next((item for item in load_local_historical(station) if item["id"] == weather_reference.get("id")), None)
            if candidate is None or weather_reference.get("source_type") != "NCPOR_HISTORICAL" or weather_reference.get("station_id") != station or weather_reference.get("temperature_c") != candidate.get("temperature_c"):
                raise HTTPException(status_code=422, detail="Historical weather reference is invalid or belongs to a different station.")
        parameters = payload.parameters.model_dump()
        result = run_scenario(baseline, parameters)
        inputs_json = json.dumps({"parameters": parameters, "weather_reference": weather_reference}, sort_keys=True)
        baseline_json = json.dumps(baseline, sort_keys=True)
        result_json = json.dumps(result, sort_keys=True)
        connection.execute("INSERT INTO scenarios (id, station, name, base_revision, model_version, created_at, inputs_json, baseline_json, result_json, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'COMPLETED')", (scenario_id, station, payload.name, revision, MODEL_VERSION, now, inputs_json, baseline_json, result_json))
        connection.execute("INSERT INTO scenario_results (scenario_id, station, base_revision, model_version, projected_json, constraints_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (scenario_id, station, revision, MODEL_VERSION, json.dumps(result["projected"], sort_keys=True), json.dumps(result["constraints"], sort_keys=True), now))
        for assumption in result["assumptions"]:
            connection.execute("INSERT INTO assumptions (scenario_id, name, value_json, unit, assumption_type) VALUES (?, ?, ?, ?, ?)", (scenario_id, assumption["name"], json.dumps(assumption["value"]), assumption["unit"], assumption["type"]))
    return {"id": scenario_id, "station": station, "name": payload.name, "base_revision": revision, "model_version": MODEL_VERSION, "created_at": now, "status": "COMPLETED", "inputs": parameters, "weather_reference": weather_reference, "baseline": baseline, "result": result}


@app.post("/hq/remote-operation-proposals")
def create_remote_operation_proposal(payload: ProposalInput, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_hq(authorization)
    proposal_id = f"ROP-{secrets.token_hex(4).upper()}"
    created_at = datetime.now(timezone.utc).isoformat()
    with database() as connection:
        scenario = connection.execute("SELECT * FROM scenarios WHERE id = ? AND station = ?", (payload.scenario_id, session["station"])).fetchone()
        if scenario is None or scenario["base_revision"] != payload.base_revision or scenario["model_version"] != payload.model_version:
            raise HTTPException(status_code=409, detail="Linked scenario does not match this station, revision, or model version.")
        connection.execute("INSERT INTO remote_operation_proposals (id, station, scenario_id, title, base_revision, model_version, status, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?)", (proposal_id, session["station"], payload.scenario_id, payload.title, payload.base_revision, payload.model_version, created_at, session["username"]))
        scenario_result = json.loads(scenario["result_json"])
        candidate = "Request a station inspection and updated observation before operational decisions; this proposal cannot control equipment."
        metadata = {"projected_constraints": scenario_result.get("constraints", []), "assumptions": scenario_result.get("assumptions", []), "uncertainties": scenario_result.get("uncertainties", []), "candidate_action": candidate, "expected_outcome": "Reduce uncertainty before an operational decision."}
        create_operation_row(connection, session, proposal_id, "ACTION_PLAN", payload.title, candidate, "P2", "station", payload.base_revision, payload.scenario_id, payload.model_version, None, None, {"source_type": "SCENARIO_ASSUMPTION", "scenario_id": payload.scenario_id}, metadata, proposal_id)
    return {"id": proposal_id, "station": session["station"], **payload.model_dump(), "status": "DRAFT", "created_at": created_at}


@app.get("/hq/overview/{station}")
def hq_overview(station: str, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_hq(authorization, station)
    with database() as connection:
        events = connection.execute("SELECT * FROM station_events WHERE station = ? ORDER BY id DESC LIMIT 8", (station,)).fetchall()
        latest = connection.execute("SELECT * FROM station_events WHERE station = ? ORDER BY id DESC LIMIT 1", (station,)).fetchone()
        requests = connection.execute("SELECT * FROM measurement_requests WHERE station = ? ORDER BY created_at DESC LIMIT 8", (station,)).fetchall()
        inspections = connection.execute("SELECT * FROM inspection_requests WHERE station = ? ORDER BY created_at DESC LIMIT 8", (station,)).fetchall()
        proposals = connection.execute("SELECT * FROM remote_operation_proposals WHERE station = ? ORDER BY created_at DESC LIMIT 8", (station,)).fetchall()
        high_water = connection.execute("SELECT COALESCE(MAX(id), 0) FROM station_events WHERE station = ?", (station,)).fetchone()[0]
        revision = station_revision(connection, station)
    revision_hash = latest["revision_hash"] if latest else hashlib.sha256(f"{station}:empty:v1".encode()).hexdigest()[:16]
    state = {key: {"value": None, "unit": unit, "source": "UNKNOWN", "source_kind": "UNKNOWN", "timestamp": None, "quality": "UNKNOWN", "revision": revision, "model_version": None, "evidence_refs": []} for key, unit in {
        "power_available": "kW", "critical_load": "kW", "deferrable_load": "kW",
        "heat_indoor": "\u00b0C", "heat_outdoor": "\u00b0C", "fuel_reserve": "L", "water_reserve": "L",
    }.items()}
    for event in reversed(events):
        try:
            payload = json.loads(event["payload"])
            if not isinstance(payload, dict):
                continue
        except (TypeError, json.JSONDecodeError):
            continue
        for key, value in payload.items():
            if key in state and value is not None:
                try:
                    evidence_refs = json.loads(event["evidence_refs"])
                    if not isinstance(evidence_refs, list):
                        evidence_refs = []
                except (TypeError, json.JSONDecodeError):
                    evidence_refs = []
                source = event["source"]
                source_lower = source.lower()
                source_kind = "DEMO_SYNTHETIC" if "synthetic" in source_lower or "emulator" in source_lower else "MANUAL_DEMO_ENTRY" if "manual" in source_lower else "UNKNOWN"
                state[key] = {"value": value, "unit": state[key]["unit"], "source": source, "source_kind": source_kind, "timestamp": event["occurred_at"], "quality": "UNKNOWN", "revision": event["id"], "model_version": event["model_version"], "evidence_refs": evidence_refs}
    return {
        "station": next(item for item in STATIONS if item["id"] == station),
        "revision": revision, "revision_hash": revision_hash, "event_high_water_mark": high_water,
        "model_version": latest["model_version"] if latest else "v0.1.0",
        "manifest_version": latest["manifest_version"] if latest else "v1",
        "state": state, "events": [dict(row) for row in events],
        "measurement_requests": [dict(row) for row in requests],
        "inspection_requests": [dict(row) for row in inspections],
        "remote_operation_proposals": [dict(row) for row in proposals],
        "last_observation": latest["occurred_at"] if latest else None,
        "last_synchronization": None, "transport": "CONNECTED", "username": session["username"],
    }


@app.get("/api/hq/command-center")
def hq_command_center(authorization: str | None = Header(default=None)) -> dict:
    """Read-only operational overview assembled from the existing station records."""
    session = authorized_session(authorization, allowed_roles={"hq"})
    station = session["station"]
    with database() as connection:
        station_row = connection.execute("SELECT * FROM stations WHERE id=?", (station,)).fetchone()
        state_rows = connection.execute("SELECT * FROM station_state WHERE station=? ORDER BY field", (station,)).fetchall()
        operations = [operation_document(connection, row) for row in connection.execute(
            "SELECT * FROM remote_operations WHERE station_id=? ORDER BY updated_at DESC LIMIT 50", (station,)).fetchall()]
        scenarios = []
        for row in connection.execute("SELECT * FROM scenarios WHERE station=? ORDER BY created_at DESC LIMIT 12", (station,)).fetchall():
            result = json.loads(row["result_json"])
            scenarios.append({"id": row["id"], "station_id": row["station"], "name": row["name"],
                "base_revision": row["base_revision"], "model_version": row["model_version"],
                "created_at": row["created_at"], "status": row["status"], "result": result})
        reconciliations = []
        for row in connection.execute("SELECT * FROM reconciliations WHERE station_id=? ORDER BY created_at DESC LIMIT 50", (station,)).fetchall():
            op = connection.execute("SELECT title,status FROM remote_operations WHERE id=?", (row["operation_id"],)).fetchone()
            reconciliations.append({"id": row["id"], "station_id": row["station_id"], "scenario_id": row["scenario_id"],
                "operation_id": row["operation_id"], "created_at": row["created_at"], "status": row["status"],
                "review_classification": row["review_classification"], "reviewed_at": row["reviewed_at"],
                "model_version": row["model_version"], "operation_title": op["title"] if op else None,
                "operation_status": op["status"] if op else None})
        observations = [dict(row) for row in connection.execute(
            "SELECT * FROM station_observations WHERE station_id=? ORDER BY recorded_at DESC LIMIT 30", (station,)).fetchall()]
        sync_groups = {row["sync_status"]: row["count"] for row in connection.execute(
            "SELECT sync_status,COUNT(*) AS count FROM sync_event_log WHERE station_id=? GROUP BY sync_status", (station,)).fetchall()}
        sync_latest = connection.execute("SELECT last_attempt_at,server_revision FROM sync_event_log WHERE station_id=? AND sync_status='SYNCED' ORDER BY last_attempt_at DESC LIMIT 1", (station,)).fetchone()
        conflicts = [dict(row) for row in connection.execute(
            "SELECT c.id,c.event_id,c.operation_id,c.client_revision,c.server_revision,c.conflict_type,e.event_type,e.created_at "
            "FROM sync_conflicts c JOIN sync_event_log e ON e.event_id=c.event_id "
            "WHERE c.station_id=? AND c.resolution IS NULL ORDER BY e.created_at DESC", (station,)).fetchall()]
        sync_events = [dict(row) for row in connection.execute(
            "SELECT event_id,operation_id,event_type,created_at,local_revision,server_revision,sync_status,payload_hash "
            "FROM sync_event_log WHERE station_id=? ORDER BY created_at DESC LIMIT 100", (station,)).fetchall()]
        audit = []
        for row in connection.execute("""SELECT e.operation_id AS record_id,e.event_type,e.actor_id,e.actor_role,e.timestamp,
            e.station_revision AS revision,e.source FROM operation_events e JOIN remote_operations o ON o.id=e.operation_id
            WHERE o.station_id=? ORDER BY e.id DESC LIMIT 12""", (station,)).fetchall():
            audit.append(dict(row))
        for row in connection.execute("""SELECT r.id AS record_id,e.event_type,e.actor_id,e.actor_role,e.occurred_at AS timestamp,
            r.current_revision AS revision,'HQ_REVIEW' AS source FROM reconciliation_events e
            JOIN reconciliations r ON r.id=e.reconciliation_id WHERE r.station_id=? ORDER BY e.id DESC LIMIT 8""", (station,)).fetchall():
            audit.append(dict(row))
        revision = station_revision(connection, station)
        weather = _weather_document(connection.execute(
            "SELECT * FROM weather_observations WHERE station_id=? AND source_type='NCPOR_OFFICIAL_OBSERVATION' ORDER BY observation_time DESC,id DESC LIMIT 1",
            (station,)).fetchone())

    state = [{"field": row["field"], "value": json.loads(row["value_json"]), "unit": row["unit"],
        "source_type": row["source_kind"], "observed_at": row["timestamp"], "quality": row["quality"],
        "revision": row["revision"], "model_version": row["model_version"]} for row in state_rows]
    pending_statuses = {"PENDING", "SYNCING", "FAILED", "CONFLICT", "BLOCKED_PENDING_PREDECESSOR"}
    pending_count = sum(count for status, count in sync_groups.items() if status in pending_statuses)
    sync_state = "SYNCED" if pending_count == 0 and sync_latest else "PENDING SYNC" if pending_count else "NO SYNC RECEIPT"
    active = [op for op in operations if op["status"] not in {"CLOSED", "DECLINED"}]
    attention = []
    status_needing_response = {"QUEUED", "DELIVERED", "SEEN", "ACCEPTED", "IN_PROGRESS", "REPORTED_BLOCKED", "REPORTED_COMPLETE", "REOPENED"}
    for op in active:
        if op["status"] in {"REPORTED_COMPLETE", "REPORTED_BLOCKED"}:
            attention.append({"kind": "HQ_REVIEW_PENDING", "label": "STATION REPORT AWAITING HQ REVIEW", "operation_id": op["id"], "title": op["title"], "priority": op["priority"]})
        elif op["status"] in status_needing_response and op["priority"] == "P1" and op["status"] in {"QUEUED", "DELIVERED", "SEEN"}:
            attention.append({"kind": "STATION_RESPONSE", "label": "P1 OPERATION AWAITING STATION RESPONSE", "operation_id": op["id"], "title": op["title"], "priority": op["priority"]})
        if op["base_revision"] != revision and op["status"] not in {"CLOSED", "DECLINED", "REVIEWED_OUTCOME"}:
            attention.append({"kind": "STALE_BASE_REVISION", "label": "STALE BASE REVISION", "operation_id": op["id"], "title": op["title"], "priority": op["priority"]})
        if op["status"] == "REPORTED_BLOCKED":
            attention.append({"kind": "BLOCKED_OPERATION", "label": "BLOCKED STATION OPERATION", "operation_id": op["id"], "title": op["title"], "priority": op["priority"]})
    for conflict in conflicts:
        attention.append({"kind": "SYNC_CONFLICT", "label": "SYNC CONFLICT", "operation_id": conflict["operation_id"], "conflict_id": conflict["id"], "priority": "P1"})
    for case in reconciliations:
        if case["status"] in {"NOT_REVIEWED", "REOPENED"}:
            attention.append({"kind": "RECONCILIATION_REQUIRED", "label": "RECONCILIATION REQUIRED", "operation_id": case["operation_id"], "reconciliation_id": case["id"], "title": case["operation_title"], "priority": "P1"})
    if not weather:
        attention.append({"kind": "WEATHER_UNAVAILABLE", "label": "NCPOR OBSERVATION UNAVAILABLE", "priority": "P2"})
    if pending_count:
        attention.append({"kind": "PENDING_SYNC", "label": f"{pending_count} SERVER-RECORDED EVENTS NEED SYNC REVIEW", "priority": "P1"})
    audit.sort(key=lambda event: event["timestamp"] or "", reverse=True)
    return {"station": dict(station_row) if station_row else next(item for item in STATIONS if item["id"] == station),
        "role": "hq", "revision": revision, "state": state, "weather": weather, "operations": operations,
        "active_operations": active, "scenarios": scenarios, "reconciliations": reconciliations,
        "observations": observations, "sync": {"state": sync_state, "pending_count": pending_count,
        "conflict_count": len(conflicts), "last_successful_sync": sync_latest["last_attempt_at"] if sync_latest else None,
        "last_server_revision": sync_latest["server_revision"] if sync_latest else None, "server_counts": sync_groups,
        "scope_note": "Only events received by the backend are represented; unsynchronized browser-local events are not visible to HQ."},
        "conflicts": conflicts, "sync_events": sync_events, "attention": attention, "audit": audit[:12], "generated_at": datetime.now(timezone.utc).isoformat()}


@app.post("/hq/measurement-requests")
def create_measurement_request(payload: MeasurementRequestInput, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_hq(authorization)
    request_id = f"MR-{secrets.token_hex(4).upper()}"
    created_at = datetime.now(timezone.utc).isoformat()
    with database() as connection:
        event_count = station_revision(connection, session["station"])
        if payload.base_revision != event_count:
            raise HTTPException(status_code=409, detail="Base revision changed. Refresh station state before saving this request.")
        if payload.scenario_id:
            scenario = connection.execute("SELECT base_revision, model_version FROM scenarios WHERE id = ? AND station = ?", (payload.scenario_id, session["station"])).fetchone()
            if scenario is None or scenario["base_revision"] != payload.base_revision or scenario["model_version"] != payload.model_version:
                raise HTTPException(status_code=409, detail="Linked scenario does not match this station, revision, or model version.")
        connection.execute("""INSERT INTO measurement_requests
            (id, station, metric, unit, reason, deadline, related_scenario, base_revision, status, created_at, created_by, scenario_id, model_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?)""",
            (request_id, session["station"], payload.metric, payload.unit, payload.reason, payload.deadline,
             payload.scenario_id or payload.related_scenario, payload.base_revision, created_at, session["username"], payload.scenario_id, payload.model_version))
        create_operation_row(connection, session, request_id, "MEASUREMENT_REQUEST", payload.metric, payload.reason, "P2", "station", payload.base_revision, payload.scenario_id, payload.model_version, payload.deadline, None, {"source_type": "MANUAL", "created_by": session["username"]}, {"metric": payload.metric, "unit": payload.unit, "deadline": payload.deadline}, request_id)
    return {"id": request_id, "station": session["station"], **payload.model_dump(), "status": "DRAFT", "created_at": created_at, "created_by": session["username"]}


@app.post("/hq/measurement-requests/{request_id}/queue")
def queue_measurement_request(request_id: str, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_hq(authorization)
    with database() as connection:
        request = connection.execute("SELECT * FROM measurement_requests WHERE id = ? AND station = ?", (request_id, session["station"])).fetchone()
        if request is None:
            raise HTTPException(status_code=404, detail="Request not found for this station")
    result = transition_operation(request_id, authorization, {"hq"}, {"REVIEWED_FOR_COMMUNICATION", "DRAFT"}, "QUEUED", "OPERATION_QUEUED", {"delivery": "PENDING_SYNC"})
    return {"id": request_id, "status": "QUEUED", "delivery": "PENDING_SYNC", "operation": result}


@app.post("/hq/inspection-requests")
def create_inspection_request(payload: InspectionRequestInput, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_hq(authorization)
    request_id = f"IR-{secrets.token_hex(4).upper()}"
    created_at = datetime.now(timezone.utc).isoformat()
    with database() as connection:
        current_revision = station_revision(connection, session["station"])
        if payload.base_revision != current_revision:
            raise HTTPException(status_code=409, detail="Base revision changed. Refresh station state before saving this request.")
        if payload.scenario_id:
            scenario = connection.execute("SELECT base_revision, model_version FROM scenarios WHERE id = ? AND station = ?", (payload.scenario_id, session["station"])).fetchone()
            if scenario is None or scenario["base_revision"] != payload.base_revision or scenario["model_version"] != payload.model_version:
                raise HTTPException(status_code=409, detail="Linked scenario does not match this station, revision, or model version.")
        connection.execute("""INSERT INTO inspection_requests
            (id, station, asset, requested_checks, reason, deadline, base_revision, status, created_at, created_by, scenario_id, model_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?)""",
            (request_id, session["station"], payload.asset, payload.requested_checks, payload.reason,
             payload.deadline, payload.base_revision, created_at, session["username"], payload.scenario_id, payload.model_version))
        create_operation_row(connection, session, request_id, "INSPECTION_REQUEST", payload.asset, payload.reason, "P2", "station", payload.base_revision, payload.scenario_id, payload.model_version, payload.deadline, None, {"source_type": "MANUAL", "created_by": session["username"]}, {"area": payload.asset, "instructions": payload.requested_checks, "deadline": payload.deadline}, request_id)
    return {"id": request_id, "station": session["station"], **payload.model_dump(), "status": "DRAFT", "created_at": created_at, "created_by": session["username"]}


@app.post("/hq/inspection-requests/{request_id}/queue")
def queue_inspection_request(request_id: str, authorization: str | None = Header(default=None)) -> dict:
    session = authorized_hq(authorization)
    with database() as connection:
        request = connection.execute("SELECT * FROM inspection_requests WHERE id = ? AND station = ?", (request_id, session["station"])).fetchone()
        if request is None:
            raise HTTPException(status_code=404, detail="Request not found for this station")
    result = transition_operation(request_id, authorization, {"hq"}, {"REVIEWED_FOR_COMMUNICATION", "DRAFT"}, "QUEUED", "OPERATION_QUEUED", {"delivery": "PENDING_SYNC"})
    return {"id": request_id, "status": "QUEUED", "delivery": "PENDING_SYNC", "operation": result}


frontend_directory = Path(__file__).resolve().parents[1] / "dist"
if frontend_directory.is_dir():
    app.mount("/", StaticFiles(directory=frontend_directory, html=True), name="frontend")

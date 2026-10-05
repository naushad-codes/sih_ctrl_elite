"""Independent station-edge API with durable local state and an outbound event journal."""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import urllib.error
import urllib.request
import uuid

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.emulator.process_model import state_at

DB_PATH = Path(os.environ.get("PRATIBIMB_EDGE_DB_PATH", Path(__file__).parent / "station_edge.db"))
EDGE_TOKEN = os.environ.get("PRATIBIMB_EDGE_TOKEN", "local-edge-token")
EDGE_STATION_TOKEN = os.environ.get("PRATIBIMB_EDGE_STATION_TOKEN", EDGE_TOKEN)
EDGE_TECHNICAL_TOKEN = os.environ.get("PRATIBIMB_EDGE_TECHNICAL_TOKEN", EDGE_TOKEN)
GATEWAY_URL = os.environ.get("PRATIBIMB_HQ_GATEWAY_URL", "http://127.0.0.1:8000/api/gateway")
GATEWAY_TOKEN = os.environ.get("PRATIBIMB_GATEWAY_TOKEN", "local-sync-token")
DEFAULT_STATION = os.environ.get("PRATIBIMB_EDGE_STATION", "maitri").lower()
ALLOWED_ORIGINS = os.environ.get("PRATIBIMB_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


@contextmanager
def database():
    db = connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    with database() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS edge_state(station_id TEXT NOT NULL, field TEXT NOT NULL, value_json TEXT NOT NULL, unit TEXT NOT NULL, source_type TEXT NOT NULL, observed_at TEXT NOT NULL, revision INTEGER NOT NULL, metadata_json TEXT NOT NULL, PRIMARY KEY(station_id,field));
        CREATE TABLE IF NOT EXISTS edge_operations(operation_id TEXT PRIMARY KEY, station_id TEXT NOT NULL, envelope_json TEXT NOT NULL, status TEXT NOT NULL, received_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS edge_observations(observation_id TEXT PRIMARY KEY, station_id TEXT NOT NULL, operation_id TEXT, metric TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL, observed_at TEXT NOT NULL, method TEXT NOT NULL, quality TEXT NOT NULL, notes TEXT NOT NULL, source_type TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS edge_reports(report_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, station_id TEXT NOT NULL, report_type TEXT NOT NULL, summary TEXT NOT NULL, observations_json TEXT NOT NULL, evidence_refs_json TEXT NOT NULL, created_at TEXT NOT NULL, source_type TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS edge_technical_findings(finding_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, station_id TEXT NOT NULL, actor_id TEXT NOT NULL, status TEXT NOT NULL, finding TEXT NOT NULL, observations_json TEXT NOT NULL, constraints_text TEXT NOT NULL, recommendation TEXT NOT NULL, blocked_reason TEXT NOT NULL, evidence_refs_json TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS edge_operation_events(event_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, station_id TEXT NOT NULL, event_type TEXT NOT NULL, actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, occurred_at TEXT NOT NULL, previous_status TEXT, new_status TEXT, revision INTEGER NOT NULL, source TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS edge_sync_meta(station_id TEXT PRIMARY KEY, last_successful_sync TEXT, last_server_revision INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS edge_outbox(event_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, station_id TEXT NOT NULL, device_id TEXT NOT NULL, operation_id TEXT NOT NULL, event_type TEXT NOT NULL, created_at TEXT NOT NULL, local_revision INTEGER NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL, sync_status TEXT NOT NULL DEFAULT 'PENDING', last_error TEXT);
        CREATE TABLE IF NOT EXISTS edge_sync_receipts(event_id TEXT PRIMARY KEY, server_event_id TEXT, server_revision INTEGER, received_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS emulator_ticks(station_id TEXT PRIMARY KEY, tick INTEGER NOT NULL, fault_at INTEGER, seed INTEGER NOT NULL, updated_at TEXT NOT NULL);
        """)
        for station in ("maitri", "bharati"):
            state = state_at(station, 0)["state"]
            for field, item in state.items():
                db.execute("INSERT OR IGNORE INTO edge_state VALUES(?,?,?,?,?,?,0,?)", (station, field, json.dumps(item["value"]), item["unit"], item["source_type"], item["observed_at"], json.dumps({k:v for k,v in item.items() if k not in {"value","unit","source_type","observed_at"}})))
            db.execute("INSERT OR IGNORE INTO emulator_ticks VALUES(?,0,NULL,26060,?)", (station, datetime.now(timezone.utc).isoformat()))


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="PRATIBIMB Station Edge", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in ALLOWED_ORIGINS if x.strip()], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def authorize(authorization: str | None, allowed_roles: set[str] | None = None):
    token = authorization.removeprefix("Bearer ") if authorization else ""
    role = "station" if token == EDGE_STATION_TOKEN else "technical" if token == EDGE_TECHNICAL_TOKEN else None
    if role is None or (allowed_roles is not None and role not in allowed_roles):
        raise HTTPException(status_code=401, detail="Station edge session required")
    return role


def machine_auth(token: str | None):
    if token != GATEWAY_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid edge gateway credential")


def iso_now():
    return datetime.now(timezone.utc).isoformat()


class ObservationInput(BaseModel):
    operation_id: str | None = None
    metric: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=80)
    unit: str = Field(min_length=1, max_length=30)
    observation_time: str = Field(min_length=1, max_length=80)
    method: str = Field(min_length=1, max_length=120)
    quality: str = "UNASSESSED"
    notes: str = Field(default="", max_length=2000)


class ActionInput(BaseModel):
    note: str = Field(default="", max_length=2000)
    summary: str = Field(default="", max_length=2000)
    reason: str = Field(default="", max_length=2000)
    acknowledge_revision: bool = False
    observation: ObservationInput | None = None


class TechnicalFindingInput(BaseModel):
    operation_id: str
    actor_id: str = "technical_engineer"
    finding: str = Field(min_length=5, max_length=4000)
    observations: dict = Field(default_factory=dict)
    constraints: str = Field(default="", max_length=2000)
    recommendation: str = Field(default="", max_length=2000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)


class TechnicalFindingDispositionInput(BaseModel):
    actor_id: str = "technical_engineer"
    summary: str = Field(default="", max_length=2000)
    reason: str = Field(default="", max_length=2000)


class EmulatorStateInput(BaseModel):
    station_id: str
    tick: int = Field(ge=0)
    seed: int = 26060
    fault_at: int | None = Field(default=None, ge=0)
    perturbation: float = 0.0


class SyncInput(BaseModel):
    station_id: str = Field(default=DEFAULT_STATION, pattern="^(maitri|bharati)$")


def enqueue(db: sqlite3.Connection, station: str, operation_id: str, event_type: str, payload: dict, revision: int):
    event_id = f"EDGE-{uuid.uuid4().hex.upper()}"
    idempotency_key = f"edge:{station}:{event_id}"
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    envelope_for_hash = json.dumps({"operation_id":operation_id,"action":event_type,"payload":payload}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(envelope_for_hash.encode()).hexdigest()
    created_at = iso_now()
    op = db.execute("SELECT envelope_json FROM edge_operations WHERE operation_id=?", (operation_id,)).fetchone()
    if op:
        envelope = json.loads(op["envelope_json"])
        # The HQ revision advances only when HQ's station-state journal says so;
        # local observation/report events do not increment that server revision.
        # Preserve the operation's base revision so stale-plan detection remains meaningful.
        revision = int(envelope.get("base_revision", 0))
    db.execute("INSERT INTO edge_outbox(event_id,idempotency_key,station_id,device_id,operation_id,event_type,created_at,local_revision,payload_hash,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)", (event_id,idempotency_key,station,f"EDGE-{station.upper()}",operation_id,event_type,created_at,revision,digest,body))
    return event_id


@app.get("/health")
def health():
    with database() as db:
        count = db.execute("SELECT COUNT(*) FROM edge_outbox WHERE sync_status='PENDING'").fetchone()[0]
    return {"service":"station-edge","database":str(DB_PATH),"pending_events":count,"status":"READY"}


@app.get("/api/emulator/status")
def emulator_status(station_id: str = DEFAULT_STATION, x_emulator_token: str | None = Header(default=None)):
    if x_emulator_token != os.environ.get("PRATIBIMB_EMULATOR_TOKEN", "local-emulator-token"):
        raise HTTPException(status_code=403, detail="Emulator credential required")
    with database() as db:
        row=db.execute("SELECT * FROM emulator_ticks WHERE station_id=?",(station_id,)).fetchone()
    return dict(row) if row else {"station_id":station_id,"tick":0,"fault_at":None,"seed":26060}


@app.get("/api/edge/state")
def get_state(station_id: str = DEFAULT_STATION, authorization: str | None = Header(default=None)):
    authorize(authorization)
    with database() as db:
        rows = db.execute("SELECT * FROM edge_state WHERE station_id=? ORDER BY field", (station_id,)).fetchall()
        revision = db.execute("SELECT COALESCE(MAX(revision),0) FROM edge_state WHERE station_id=?", (station_id,)).fetchone()[0]
    return {"station_id":station_id,"revision":revision,"authority":"STATION_EDGE_LOCAL","values":[{"field":r["field"],"value":json.loads(r["value_json"]),"unit":r["unit"],"source_type":r["source_type"],"observed_at":r["observed_at"],"revision":r["revision"],"metadata":json.loads(r["metadata_json"])} for r in rows]}


@app.get("/api/edge/operations")
def list_operations(station_id: str = DEFAULT_STATION, authorization: str | None = Header(default=None)):
    role = authorize(authorization, {"station", "technical"})
    with database() as db:
        rows = db.execute("SELECT * FROM edge_operations WHERE station_id=? ORDER BY received_at DESC", (station_id,)).fetchall()
    operations=[{**dict(r),"envelope":json.loads(r["envelope_json"])} for r in rows]
    if role == "station":
        operations=[op for op in operations if op["envelope"].get("assigned_role") in {"station","both"}]
    else:
        operations=[op for op in operations if op["envelope"].get("assigned_role") in {"technical","both"}]
    return {"station_id":station_id,"operations":operations}


@app.get("/api/edge/observations")
def list_observations(station_id: str = DEFAULT_STATION, authorization: str | None = Header(default=None)):
    role = authorize(authorization, {"station", "technical"})
    with database() as db:
        rows = db.execute("SELECT * FROM edge_observations WHERE station_id=? ORDER BY observed_at DESC", (station_id,)).fetchall()
    return {"station_id":station_id,"observations":[dict(r) for r in rows]}


@app.get("/api/edge/outbox")
def get_outbox(station_id: str = DEFAULT_STATION, authorization: str | None = Header(default=None)):
    authorize(authorization)
    with database() as db:
        rows = db.execute("SELECT * FROM edge_outbox WHERE station_id=? ORDER BY created_at", (station_id,)).fetchall()
    return {"station_id":station_id,"events":[{**dict(r),"payload":json.loads(r["payload_json"])} for r in rows]}


@app.get("/api/edge/status")
def edge_status(station_id: str = DEFAULT_STATION, authorization: str | None = Header(default=None)):
    authorize(authorization)
    with database() as db:
        counts = {row["sync_status"]: row["n"] for row in db.execute("SELECT sync_status,COUNT(*) n FROM edge_outbox WHERE station_id=? GROUP BY sync_status", (station_id,)).fetchall()}
        oldest = db.execute("SELECT MIN(created_at) FROM edge_outbox WHERE station_id=? AND sync_status IN ('PENDING','FAILED','CONFLICT')", (station_id,)).fetchone()[0]
        meta = db.execute("SELECT * FROM edge_sync_meta WHERE station_id=?", (station_id,)).fetchone()
    try:
        transport = gateway_request(f"/transport-status/{station_id}")
        link = transport.get("status", "CONNECTED")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        link = "OFFLINE"
    pending = counts.get("PENDING", 0) + counts.get("FAILED", 0)
    conflict = counts.get("CONFLICT", 0) + counts.get("REVISION_CONFLICT", 0)
    failed = counts.get("REJECTED", 0) + counts.get("FAILED", 0)
    if conflict:
        connection = "CONFLICT"
    elif link == "OFFLINE":
        connection = "OFFLINE"
    elif link == "DEGRADED" or failed:
        connection = "DEGRADED"
    elif pending:
        connection = "PENDING SYNC"
    else:
        connection = "SYNCED"
    return {"station_id":station_id,"connection":connection,"gateway_status":link,"pending":pending,"oldest_pending_at":oldest,
            "last_successful_sync":meta["last_successful_sync"] if meta else None,"last_server_revision":meta["last_server_revision"] if meta else 0,
            "conflicts":conflict,"failed":failed,"source":"STATION_EDGE"}


@app.get("/api/edge/technical-findings")
def list_technical_findings(station_id: str = DEFAULT_STATION, operation_id: str | None = None, authorization: str | None = Header(default=None)):
    authorize(authorization)
    with database() as db:
        if operation_id:
            rows = db.execute("SELECT * FROM edge_technical_findings WHERE station_id=? AND operation_id=? ORDER BY created_at DESC", (station_id, operation_id)).fetchall()
        else:
            rows = db.execute("SELECT * FROM edge_technical_findings WHERE station_id=? ORDER BY created_at DESC", (station_id,)).fetchall()
    return {"station_id":station_id,"findings":[{**dict(row),"observations":json.loads(row["observations_json"]),"evidence_refs":json.loads(row["evidence_refs_json"])} for row in rows]}


def append_local_event(db: sqlite3.Connection, station: str, operation_id: str, event_type: str, actor_id: str,
                       previous_status: str | None, new_status: str | None, revision: int, payload: dict):
    event_id = f"EV-EDGE-{uuid.uuid4().hex[:12].upper()}"
    db.execute("INSERT INTO edge_operation_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (event_id, operation_id, station, event_type, actor_id, "technical" if event_type.startswith("TECHNICAL_") else "station",
                iso_now(), previous_status, new_status, revision, "STATION_EDGE", json.dumps(payload, sort_keys=True)))
    return event_id


@app.post("/api/edge/observations")
def record_observation(payload: ObservationInput, station_id: str = DEFAULT_STATION, authorization: str | None = Header(default=None)):
    role = authorize(authorization, {"station", "technical"})
    now = iso_now()
    observation_id = f"OBS-EDGE-{uuid.uuid4().hex[:10].upper()}"
    with database() as db:
        revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM edge_state WHERE station_id=?", (station_id,)).fetchone()[0]
        db.execute("INSERT INTO edge_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)", (observation_id,station_id,payload.operation_id,payload.metric,payload.value,payload.unit,payload.observation_time,payload.method,payload.quality,payload.notes,"MANUAL"))
        operation = db.execute("SELECT * FROM edge_operations WHERE operation_id=? AND station_id=?", (payload.operation_id,station_id)).fetchone() if payload.operation_id else None
        if operation is None:
            raise HTTPException(status_code=409, detail="Select an operation already received by this station before recording an observation")
        envelope=json.loads(operation["envelope_json"])
        if role == "station" and envelope.get("assigned_role") not in {"station","both"}:
            raise HTTPException(status_code=403, detail="Operation is not assigned to station operations")
        if role == "technical" and envelope.get("assigned_role") not in {"technical","both"}:
            raise HTTPException(status_code=403, detail="Operation is not assigned to technical engineering")
        event_type = "observation" if role == "station" else "technical-observation"
        enqueue(db,station_id,payload.operation_id,event_type,{**payload.model_dump(),"confidence":"UNASSESSED","observation_id":observation_id,"actor_id":"technical_engineer" if role=="technical" else "station_operator"},revision)
        append_local_event(db,station_id,payload.operation_id,"TECHNICAL_OBSERVATION_RECORDED" if role=="technical" else "STATION_OBSERVATION_RECORDED","technical_engineer" if role=="technical" else "station_operator",None,None,revision,{"observation_id":observation_id,"metric":payload.metric,"source_type":"MANUAL"})
    return {"id":observation_id,"station_id":station_id,"source_type":"MANUAL","stored_locally":True,"sync_status":"PENDING"}


@app.post("/api/edge/operations/{operation_id}/{action}")
def act(operation_id: str, action: str, payload: ActionInput, station_id: str = DEFAULT_STATION, authorization: str | None = Header(default=None)):
    authorize(authorization, {"station"})
    transitions = {"seen":{"DELIVERED":"SEEN"},"accept":{"DELIVERED":"ACCEPTED","SEEN":"ACCEPTED"},"decline":{"DELIVERED":"DECLINED","SEEN":"DECLINED"},"clarification":{"DELIVERED":"CLARIFICATION_REQUESTED","SEEN":"CLARIFICATION_REQUESTED"},"progress":{"ACCEPTED":"IN_PROGRESS"},"report-complete":{"IN_PROGRESS":"REPORTED_COMPLETE"},"report-blocked":{"IN_PROGRESS":"REPORTED_BLOCKED"}}
    if action not in transitions:
        raise HTTPException(status_code=404, detail="Unknown local operation action")
    now = iso_now()
    with database() as db:
        row = db.execute("SELECT * FROM edge_operations WHERE operation_id=? AND station_id=?", (operation_id,station_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Operation has not been delivered to this edge")
        new_status = transitions[action].get(row["status"])
        if new_status is None:
            raise HTTPException(status_code=409, detail=f"Cannot {action} an operation in {row['status']} state")
        if action in {"report-complete", "report-blocked"} and len(payload.summary.strip()) < 5:
            raise HTTPException(status_code=422, detail="A concise outcome summary is required")
        event_payload = payload.model_dump(exclude_none=True)
        previous_status = row["status"]
        db.execute("UPDATE edge_operations SET status=?,updated_at=? WHERE operation_id=?", (new_status,now,operation_id))
        if action in {"report-complete","report-blocked"} and payload.observation:
            obs = payload.observation
            obs_id=f"OBS-EDGE-{uuid.uuid4().hex[:10].upper()}"
            db.execute("INSERT INTO edge_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)", (obs_id,station_id,operation_id,obs.metric,obs.value,obs.unit,obs.observation_time,obs.method,obs.quality,obs.notes,"MANUAL"))
            enqueue(db,station_id,operation_id,"observation",obs.model_dump(exclude_none=True),revision=0)
            event_payload.pop("observation",None)
            event_payload["observations"] = {"manual_observation_id":obs_id,"metric":obs.metric,"value":obs.value,"unit":obs.unit}
        revision = db.execute("SELECT COALESCE(MAX(revision),0) FROM edge_state WHERE station_id=?", (station_id,)).fetchone()[0]
        report_id = None
        if action in {"report-complete", "report-blocked"}:
            report_id = f"RPT-EDGE-{uuid.uuid4().hex[:10].upper()}"
            db.execute("INSERT INTO edge_reports VALUES(?,?,?,?,?,?,?,?,?)", (report_id,operation_id,station_id,new_status,payload.summary.strip(),json.dumps(payload.observation.model_dump() if payload.observation else {},sort_keys=True),"[]",now,"MANUAL"))
            event_payload = {"note":payload.summary.strip(),"observations":payload.observation.model_dump() if payload.observation else {},"evidence_ids":[],"acknowledge_revision":payload.acknowledge_revision,"report_id":report_id}
        event_id = enqueue(db,station_id,operation_id,action,event_payload,revision)
        append_local_event(db,station_id,operation_id,action.replace("-","_").upper(),"station-operator",previous_status,new_status,revision,{"outbox_event_id":event_id,"report_id":report_id,"payload":event_payload})
    return {"operation_id":operation_id,"status":new_status,"event_id":event_id,"report_id":report_id,"sync_status":"PENDING","stored_locally":True}


@app.post("/api/edge/technical-findings")
def create_edge_technical_finding(payload: TechnicalFindingInput, station_id: str = DEFAULT_STATION, authorization: str | None = Header(default=None)):
    role = authorize(authorization, {"technical"})
    finding_id = f"TF-EDGE-{uuid.uuid4().hex[:10].upper()}"
    now = iso_now()
    with database() as db:
        operation = db.execute("SELECT * FROM edge_operations WHERE operation_id=? AND station_id=?", (payload.operation_id, station_id)).fetchone()
        if operation is None:
            raise HTTPException(status_code=404, detail="Technical work must be linked to an operation received by this Edge")
        envelope = json.loads(operation["envelope_json"])
        if envelope.get("assigned_role") not in {"technical", "both"}:
            raise HTTPException(status_code=403, detail="Operation is not assigned to the technical role")
        revision = db.execute("SELECT COALESCE(MAX(revision),0) FROM edge_state WHERE station_id=?", (station_id,)).fetchone()[0]
        db.execute("INSERT INTO edge_technical_findings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (finding_id,payload.operation_id,station_id,payload.actor_id,"OPEN",payload.finding.strip(),json.dumps(payload.observations,sort_keys=True),payload.constraints,payload.recommendation,"",json.dumps(payload.evidence_refs),revision,now,now))
        body = {**payload.model_dump(),"finding_id":finding_id,"status":"OPEN","revision":revision,"created_at":now}
        event_id = enqueue(db,station_id,payload.operation_id,"technical-finding-create",body,revision)
        append_local_event(db,station_id,payload.operation_id,"TECHNICAL_FINDING_CREATED",payload.actor_id,None,"OPEN",revision,{"finding_id":finding_id,"outbox_event_id":event_id})
    return {"finding_id":finding_id,"operation_id":payload.operation_id,"status":"OPEN","stored_locally":True,"sync_status":"PENDING","revision":revision}


@app.post("/api/edge/technical-findings/{finding_id}/{disposition}")
def dispose_edge_technical_finding(finding_id: str, disposition: str, payload: TechnicalFindingDispositionInput,
                                   station_id: str = DEFAULT_STATION, authorization: str | None = Header(default=None)):
    authorize(authorization, {"technical"})
    if disposition not in {"complete", "blocked"}:
        raise HTTPException(status_code=404, detail="Technical finding action not found")
    status = "COMPLETE" if disposition == "complete" else "BLOCKED"
    if status == "COMPLETE" and len(payload.summary.strip()) < 5:
        raise HTTPException(status_code=422, detail="A completion summary is required")
    if status == "BLOCKED" and len(payload.reason.strip()) < 5:
        raise HTTPException(status_code=422, detail="A clear reason is required to mark the finding blocked")
    now = iso_now()
    with database() as db:
        finding = db.execute("SELECT * FROM edge_technical_findings WHERE finding_id=? AND station_id=?", (finding_id,station_id)).fetchone()
        if finding is None:
            raise HTTPException(status_code=404, detail="Technical finding not found")
        if finding["actor_id"] != payload.actor_id:
            raise HTTPException(status_code=403, detail="Only the finding author may set its disposition")
        if finding["status"] != "OPEN":
            raise HTTPException(status_code=409, detail="Technical finding is already disposed")
        db.execute("UPDATE edge_technical_findings SET status=?,blocked_reason=?,updated_at=? WHERE finding_id=?", (status,payload.reason.strip() if status=="BLOCKED" else "",now,finding_id))
        op = db.execute("SELECT * FROM edge_operations WHERE operation_id=?", (finding["operation_id"],)).fetchone()
        envelope = json.loads(op["envelope_json"])
        revision = int(envelope.get("base_revision",0))
        body = {**payload.model_dump(),"finding_id":finding_id,"status":status,"updated_at":now}
        event_id = enqueue(db,station_id,finding["operation_id"],"technical-finding-disposition",body,revision)
        append_local_event(db,station_id,finding["operation_id"],f"TECHNICAL_FINDING_{status}",payload.actor_id,"OPEN",status,revision,{"finding_id":finding_id,"outbox_event_id":event_id,**body})
    return {"finding_id":finding_id,"operation_id":finding["operation_id"],"status":status,"stored_locally":True,"sync_status":"PENDING"}


def gateway_request(path: str, body: dict | None = None):
    if os.environ.get("PRATIBIMB_EDGE_TRANSPORT", "CONNECTED").upper() == "OFFLINE":
        raise urllib.error.URLError("Transport is locally disabled")
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"{GATEWAY_URL}{path}", data=data, method="GET" if data is None else "POST", headers={"X-Sync-Token":GATEWAY_TOKEN,"Content-Type":"application/json"})
    with urllib.request.urlopen(request, timeout=4) as response:
        return json.loads(response.read().decode())


@app.post("/api/edge/sync")
def synchronize(payload: SyncInput, authorization: str | None = Header(default=None)):
    authorize(authorization)
    station = payload.station_id
    with database() as db:
        rows = db.execute("SELECT * FROM edge_outbox WHERE station_id=? AND sync_status IN ('PENDING','FAILED') ORDER BY created_at", (station,)).fetchall()
        events = [{"event_id":r["event_id"],"idempotency_key":r["idempotency_key"],"station_id":station,"device_id":r["device_id"],"operation_id":r["operation_id"],"event_type":r["event_type"],"created_at":r["created_at"],"local_revision":r["local_revision"],"payload_hash":r["payload_hash"],"payload":json.loads(r["payload_json"])} for r in rows]
    try:
        sent = gateway_request("/ingest", {"station_id":station,"device_id":f"EDGE-{station.upper()}","base_server_revision":max((event["local_revision"] for event in events),default=0),"events":events})
        with database() as db:
            for result in sent.get("accepted",[]) + sent.get("already_applied",[]):
                db.execute("UPDATE edge_outbox SET sync_status='SYNCED',last_error=NULL WHERE event_id=?", (result["event_id"],))
                db.execute("INSERT OR IGNORE INTO edge_sync_receipts VALUES(?,?,?,?)", (result["event_id"],result.get("server_event_id"),result.get("server_revision"),iso_now()))
            for result in sent.get("conflicts",[]) + sent.get("rejected",[]):
                db.execute("UPDATE edge_outbox SET sync_status=?,last_error=? WHERE event_id=?", (result.get("status","FAILED"),result.get("error_message") or result.get("error_code"),result["event_id"]))
        outbox = gateway_request(f"/outbox/{station}")
        received=[]
        with database() as db:
            for envelope in outbox.get("operations",[]):
                op_id=envelope["id"]
                db.execute("INSERT OR IGNORE INTO edge_operations VALUES(?,?,?,'DELIVERED',?,?)", (op_id,station,json.dumps(envelope,sort_keys=True),iso_now(),iso_now()))
                received.append(op_id)
        ack = gateway_request("/ack", {"station_id":station,"operation_ids":received}) if received else {"delivered":[]}
        return {"transport":"CONNECTED","sent":sent,"received_operation_ids":received,"acknowledgement":ack,"pending_count":sum(1 for e in events if e["event_id"] not in {x["event_id"] for x in sent.get("accepted",[])+sent.get("already_applied",[])})}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
        with database() as db:
            db.executemany("UPDATE edge_outbox SET sync_status='PENDING',last_error=? WHERE event_id=?", [(str(error),event["event_id"]) for event in events])
        raise HTTPException(status_code=503, detail=f"Sync gateway unavailable; local state and outbox are retained: {error}")


@app.post("/api/edge/emulator/tick")
def emulator_tick(payload: EmulatorStateInput, x_emulator_token: str | None = Header(default=None)):
    if x_emulator_token != os.environ.get("PRATIBIMB_EMULATOR_TOKEN", "local-emulator-token"):
        raise HTTPException(status_code=403, detail="Emulator credential required")
    snapshot = state_at(payload.station_id,payload.tick,payload.seed,payload.fault_at,payload.perturbation)
    now = iso_now()
    with database() as db:
        old = db.execute("SELECT COALESCE(MAX(revision),0) FROM edge_state WHERE station_id=?", (payload.station_id,)).fetchone()[0]
        revision = old + 1
        db.execute("INSERT INTO emulator_ticks VALUES(?,?,?,?,?) ON CONFLICT(station_id) DO UPDATE SET tick=excluded.tick,fault_at=excluded.fault_at,seed=excluded.seed,updated_at=excluded.updated_at", (payload.station_id,payload.tick,payload.fault_at,payload.seed,now))
        for field,item in snapshot["state"].items():
            metadata={k:v for k,v in item.items() if k not in {"value","unit","source_type","observed_at"}}
            db.execute("INSERT INTO edge_state VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(station_id,field) DO UPDATE SET value_json=excluded.value_json,unit=excluded.unit,source_type=excluded.source_type,observed_at=excluded.observed_at,revision=excluded.revision,metadata_json=excluded.metadata_json",(payload.station_id,field,json.dumps(item["value"]),item["unit"],item["source_type"],item["observed_at"],revision,json.dumps(metadata)))
    return {"station_id":payload.station_id,"revision":revision,"tick":payload.tick,"fault_injected":snapshot["fault_injected"],"state":snapshot["state"]}

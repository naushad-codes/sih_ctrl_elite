import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from backend import main
from backend.data.providers.ncpor_official import NCPOROfficialProvider


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = main.DB_PATH
        main.DB_PATH = Path(self.temp.name) / "workflow.db"
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()
        login = self.client.post("/auth/login", json={"username": "admin_hq", "password": "123", "station": "maitri", "role": "hq"})
        self.hq = {"Authorization": f"Bearer {login.json()['token']}"}
        station_login = self.client.post("/auth/login", json={"username": "admin_station", "password": "123", "station": "maitri", "role": "station"})
        self.station = {"Authorization": f"Bearer {station_login.json()['token']}"}

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        main.DB_PATH = self.db_path
        self.temp.cleanup()

    def test_demo_authentication_roles_and_server_session_logout(self):
        for username,role in (("admin_hq","hq"),("admin_station","station"),("admin_tech","technical")):
            response=self.client.post("/auth/login",json={"username":username,"password":"123","station":"maitri","role":role})
            self.assertEqual(response.status_code,200,response.text)
            token=response.json()["token"]
            signed_out=self.client.post("/auth/logout",headers={"Authorization":f"Bearer {token}"})
            self.assertEqual(signed_out.json()["status"],"SIGNED_OUT")
            revoked=self.client.get("/api/operations",headers={"Authorization":f"Bearer {token}"})
            self.assertEqual(revoked.status_code,401)
        wrong_password=self.client.post("/auth/login",json={"username":"admin_hq","password":"wrong","station":"maitri","role":"hq"})
        self.assertEqual(wrong_password.status_code,401)
        mismatched_role=self.client.post("/auth/login",json={"username":"admin_tech","password":"123","station":"maitri","role":"hq"})
        self.assertEqual(mismatched_role.status_code,401)

    def test_command_center_is_station_scoped_and_aggregates_persisted_records(self):
        scenario = self.client.post("/hq/scenarios/maitri", headers=self.hq, json={
            "name": "CHP Unavailable", "base_revision": 0, "model_version": "v0.2.0-causal-accounting",
            "parameters": {"chp_available": False, "fuel_consumption_lph": 9.5, "critical_load_kw": 100,
                "deferrable_load_kw": 20, "outdoor_temperature_c": -24, "water_demand_l_per_day": 1000, "duration_hours": 24},
        })
        self.assertEqual(scenario.status_code, 200, scenario.text)
        operation = self.client.post("/api/operations", headers={**self.hq, "Idempotency-Key": "cc-operation"}, json={
            "operation_type": "INSPECTION_REQUEST", "title": "Inspect thermal plant", "description": "Check CHP availability",
            "priority": "P1", "assigned_role": "station", "base_revision": 0, "scenario_id": scenario.json()["id"],
            "model_version": "v0.2.0-causal-accounting", "metadata": {"asset": "CHP", "requested_checks": "Availability"},
        })
        self.assertEqual(operation.status_code, 200, operation.text)
        self.client.post(f"/api/operations/{operation.json()['id']}/review", headers=self.hq, json={"note":"Reviewed for communication"})
        self.client.post(f"/api/operations/{operation.json()['id']}/queue", headers=self.hq)
        overview = self.client.get("/api/hq/command-center", headers=self.hq)
        self.assertEqual(overview.status_code, 200, overview.text)
        result = overview.json()
        self.assertEqual(result["station"]["id"], "maitri")
        self.assertEqual(result["revision"], 0)
        self.assertEqual(result["scenarios"][0]["id"], scenario.json()["id"])
        self.assertEqual(result["active_operations"][0]["id"], operation.json()["id"])
        self.assertIn("STATION_RESPONSE", [item["kind"] for item in result["attention"]])
        self.assertEqual(result["sync"]["conflict_count"], 0)
        self.assertIsNone(result["weather"])
        self.assertIn("not visible to HQ", result["sync"]["scope_note"])

        station_access = self.client.get("/api/hq/command-center", headers=self.station)
        self.assertEqual(station_access.status_code, 403)
        bharati_login = self.client.post("/auth/login", json={"username": "admin_hq", "password": "123", "station": "bharati", "role": "hq"}).json()
        bharati = self.client.get("/api/hq/command-center", headers={"Authorization": f"Bearer {bharati_login['token']}"}).json()
        self.assertEqual(bharati["station"]["id"], "bharati")
        self.assertEqual(bharati["operations"], [])

    def test_command_center_environment_has_official_provenance_metadata(self):
        observation = {"station_id":"maitri", "observation_time":"2026-10-03T21:00:00+00:00", "temperature_c":-15.4,
            "pressure_hpa":1002.0, "relative_humidity_pct":78.0, "wind_speed":6.2, "wind_speed_unit":"m/s",
            "wind_direction_deg":170.0, "source":"NCPOR", "source_type":"NCPOR_OFFICIAL_OBSERVATION", "quality":"GOOD",
            "fetched_at":"2026-10-03T21:05:00+00:00"}
        with patch.object(main.NCPOR_PROVIDER, "fetch_station_observation", return_value=observation):
            self.client.post("/api/weather/official/maitri/refresh", headers=self.hq)
        result = self.client.get("/api/hq/command-center", headers=self.hq).json()
        self.assertEqual(result["weather"]["source_type"], "NCPOR_OFFICIAL_OBSERVATION")
        self.assertEqual(result["weather"]["observation_time"], observation["observation_time"])
        self.assertEqual(result["weather"]["fetched_at"], observation["fetched_at"])

    def test_official_weather_refresh_caches_for_ten_minutes_and_force_refreshes(self):
        observation = {"station_id":"maitri", "observation_time":"2026-10-04T10:00:00+00:00", "temperature_c":-16.2,
            "pressure_hpa":958.0, "relative_humidity_pct":42.0, "wind_speed":7.1, "wind_speed_unit":"m/s",
            "wind_direction_deg":170.0, "source":"NCPOR", "source_type":"NCPOR_OFFICIAL_OBSERVATION", "quality":"GOOD",
            "fetched_at":datetime.now(timezone.utc).isoformat()}
        with patch.object(main.NCPOR_PROVIDER, "fetch_station_observation", return_value=observation) as fetch:
            first = self.client.post("/api/weather/official/maitri/refresh", headers=self.hq)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertTrue(first.json()["refreshed"])
            self.assertEqual(fetch.call_count, 1)
        with patch.object(main.NCPOR_PROVIDER, "fetch_station_observation") as fetch:
            cached = self.client.post("/api/weather/official/maitri/refresh", headers=self.hq)
            self.assertEqual(cached.status_code, 200, cached.text)
            self.assertFalse(cached.json()["refreshed"])
            self.assertLess(cached.json()["cache_age_seconds"], 600)
            fetch.assert_not_called()
        with patch.object(main.NCPOR_PROVIDER, "fetch_station_observation", return_value=observation) as fetch:
            forced = self.client.post("/api/weather/official/maitri/refresh?force=true", headers=self.hq)
            self.assertEqual(forced.status_code, 200, forced.text)
            self.assertTrue(forced.json()["refreshed"])
            fetch.assert_called_once()

    def test_command_center_outcome_counts_and_attention_follow_reconciliation_state(self):
        scenario = self.client.post("/hq/scenarios/maitri", headers=self.hq, json={
            "name":"CHP Unavailable", "base_revision":0, "model_version":"v0.2.0-causal-accounting",
            "parameters":{"chp_available":False,"fuel_consumption_lph":9.5,"critical_load_kw":100,"deferrable_load_kw":20,
                "outdoor_temperature_c":-24,"water_demand_l_per_day":1000,"duration_hours":24},
        }).json()
        op = self.client.post("/api/operations", headers={**self.hq,"Idempotency-Key":"cc-reconciliation"}, json={
            "operation_type":"INSPECTION_REQUEST","title":"Thermal plant inspection","description":"Check CHP state",
            "priority":"P1","assigned_role":"station","base_revision":0,"scenario_id":scenario["id"],"model_version":"v0.2.0-causal-accounting",
            "metadata":{"asset":"CHP","requested_checks":"Availability"},
        }).json()
        self.client.post(f"/api/operations/{op['id']}/review",headers=self.hq,json={"note":"Reviewed"})
        self.client.post(f"/api/operations/{op['id']}/queue",headers=self.hq)
        self.client.post(f"/api/station/operations/{op['id']}/receive",headers=self.station)
        self.client.post(f"/api/operations/{op['id']}/accept",headers=self.station,json={"note":"Accepted"})
        self.client.post(f"/api/operations/{op['id']}/progress",headers=self.station,json={"note":"Started"})
        observation = {"metric":"CHP Availability","value":"Unavailable","unit":"state","observation_time":"2026-10-04T10:00:00+05:30","method":"Manual inspection","quality":"UNASSESSED","confidence":"MEDIUM","notes":"Operator check"}
        saved = self.client.post(f"/api/station/operations/{op['id']}/observations",headers=self.station,json=observation).json()
        self.client.post(f"/api/operations/{op['id']}/report-complete",headers=self.station,json={"observations":{"summary":"Inspection complete"},"acknowledge_revision":True})
        rec = self.client.post(f"/api/reconciliation/{op['id']}/create",headers=self.hq,json={}).json()
        before = self.client.get("/api/hq/command-center",headers=self.hq).json()
        self.assertEqual(before["reconciliations"][0]["status"],"NOT_REVIEWED")
        self.assertEqual(sum(x["status"] in {"NOT_REVIEWED","REOPENED"} for x in before["reconciliations"]),1)
        self.assertIn("RECONCILIATION_REQUIRED",[x["kind"] for x in before["attention"]])
        self.assertEqual(before["observations"][0]["id"],saved["id"])
        self.assertEqual(before["observations"][0]["source_type"],"MANUAL")
        provenance = self.client.get(f"/api/reconciliation/{rec['id']}/provenance",headers=self.hq).json()
        self.assertEqual(provenance["chain"][0]["id"],scenario["id"])
        self.assertEqual(provenance["chain"][1]["id"],op["id"])
        self.assertEqual(provenance["chain"][2]["id"],saved["id"])
        reviewed = self.client.post(f"/api/reconciliation/{rec['id']}/review",headers=self.hq,json={"classification":"CONSISTENT_WITH_PROJECTION","notes":"Manual report reviewed."})
        self.assertEqual(reviewed.json()["status"],"REVIEWED")
        after = self.client.get("/api/hq/command-center",headers=self.hq).json()
        self.assertEqual(sum(x["status"]=="REVIEWED" for x in after["reconciliations"]),1)
        self.assertNotIn("RECONCILIATION_REQUIRED",[x["kind"] for x in after["attention"]])

    def test_technical_engineer_context_observation_report_and_evidence_are_role_scoped(self):
        scenario = self.client.post("/hq/scenarios/maitri", headers=self.hq, json={
            "name":"CHP Unavailable", "base_revision":0, "model_version":"v0.2.0-causal-accounting",
            "parameters":{"chp_available":False,"fuel_consumption_lph":9.5,"critical_load_kw":100,"deferrable_load_kw":20,
                "outdoor_temperature_c":-24,"water_demand_l_per_day":1000,"duration_hours":24},
        }).json()
        op = self.client.post("/api/operations", headers={**self.hq,"Idempotency-Key":"technical-role-task"}, json={
            "operation_type":"INSPECTION_REQUEST","title":"Thermal plant technical inspection","description":"Record a manual technical finding",
            "priority":"P2","assigned_role":"technical","base_revision":0,"scenario_id":scenario["id"],"model_version":"v0.2.0-causal-accounting",
            "metadata":{"asset":"Thermal plant","requested_checks":"CHP availability"},
        }).json()
        reviewed = self.client.post(f"/api/operations/{op['id']}/review", headers=self.hq, json={"note":"Reviewed for station communication"})
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        queued = self.client.post(f"/api/operations/{op['id']}/queue", headers=self.hq)
        self.assertEqual(queued.status_code, 200, queued.text)
        tech_login=self.client.post("/auth/login",json={"username":"admin_tech","password":"123","station":"maitri","role":"technical"}).json()
        tech={"Authorization":f"Bearer {tech_login['token']}"}
        context=self.client.get(f"/api/technical/context/{op['id']}",headers=tech)
        self.assertEqual(context.status_code,200,context.text)
        self.assertEqual(context.json()["scenario"]["id"],scenario["id"])
        self.assertIn(op["id"],[x["id"] for x in self.client.get("/api/operations",headers=tech).json()["operations"]])
        observation=self.client.post(f"/api/station/operations/{op['id']}/observations",headers=tech,json={
            "metric":"CHP Availability","value":"Unavailable","unit":"state","observation_time":"2026-10-04T10:00:00+05:30",
            "method":"Manual technical inspection","quality":"UNASSESSED","confidence":"MEDIUM","notes":"Visual inspection only."})
        self.assertEqual(observation.status_code,200,observation.text)
        evidence=self.client.post(f"/api/operations/{op['id']}/evidence",headers=tech,json={"source_type":"MANUAL","label":"Inspection note","metadata":{"source":"TECHNICAL_ENGINEER"}})
        self.assertEqual(evidence.status_code,200,evidence.text)
        report=self.client.post(f"/api/operations/{op['id']}/technical-observation",headers=tech,json={
            "note":"Technical report submitted.","observations":{"finding":"CHP reported unavailable","source":"MANUAL"},"evidence_ids":[evidence.json()["id"]]})
        self.assertEqual(report.status_code,200,report.text)
        self.assertEqual(report.json()["latest_report"]["report_type"],"TECHNICAL_OBSERVATION")
        self.assertEqual(report.json()["latest_report"]["evidence_ids"],[evidence.json()["id"]])
        refreshed=self.client.get(f"/api/operations/{op['id']}",headers=tech).json()
        self.assertEqual(refreshed["evidence"][0]["id"],evidence.json()["id"])
        forbidden=self.client.get("/api/technical/context/unknown",headers=self.station)
        self.assertEqual(forbidden.status_code,403)
        bharati_login=self.client.post("/auth/login",json={"username":"admin_hq","password":"123","station":"bharati","role":"hq"}).json()
        bharati_headers={"Authorization":f"Bearer {bharati_login['token']}"}
        other_station=self.client.post("/api/operations",headers={**bharati_headers,"Idempotency-Key":"technical-other-station"},json={
            "operation_type":"INSPECTION_REQUEST","title":"Bharati technical inspection","assigned_role":"technical","base_revision":0,
            "metadata":{"asset":"Thermal plant","requested_checks":"Availability"}}).json()
        isolated=self.client.get(f"/api/technical/context/{other_station['id']}",headers=tech)
        self.assertEqual(isolated.status_code,404)
        station_transition=self.client.post(f"/api/operations/{op['id']}/accept",headers=tech,json={"note":"Not permitted"})
        self.assertEqual(station_transition.status_code,403)

    def test_technical_finding_disposition_is_separate_from_operation_lifecycle(self):
        op=self.client.post("/api/operations",headers={**self.hq,"Idempotency-Key":"technical-finding-op"},json={
            "operation_type":"INSPECTION_REQUEST","title":"Thermal system inspection","assigned_role":"technical","base_revision":0,
            "metadata":{"asset":"CHP","requested_checks":"Availability"}}).json()
        self.client.post(f"/api/operations/{op['id']}/queue",headers=self.hq)
        tech_login=self.client.post("/auth/login",json={"username":"admin_tech","password":"123","station":"maitri","role":"technical"}).json()
        tech={"Authorization":f"Bearer {tech_login['token']}"}
        incomplete=self.client.post(f"/api/technical/findings/{op['id']}",headers=tech,json={"finding":""})
        self.assertEqual(incomplete.status_code,422)
        finding=self.client.post(f"/api/technical/findings/{op['id']}",headers=tech,json={
            "finding":"CHP reported available by visual inspection","observations":{"availability":"Available","source":"MANUAL"},
            "constraints":"No load test was performed","recommendation":"Confirm during next scheduled plant check","evidence_refs":[]})
        self.assertEqual(finding.status_code,200,finding.text)
        self.assertEqual(finding.json()["status"],"OPEN")
        blocked=self.client.post(f"/api/technical/findings/{finding.json()['id']}/blocked",headers=tech,json={"summary":"Inspection could not be completed","reason":""})
        self.assertEqual(blocked.status_code,422)
        completed=self.client.post(f"/api/technical/findings/{finding.json()['id']}/complete",headers=tech,json={"summary":"Visual checks recorded; no visible issue reported."})
        self.assertEqual(completed.status_code,200,completed.text)
        self.assertEqual(completed.json()["status"],"COMPLETE")
        self.assertEqual([x["event_type"] for x in completed.json()["events"]],["TECHNICAL_FINDING_CREATED","TECHNICAL_FINDING_COMPLETE"])
        current=self.client.get(f"/api/operations/{op['id']}",headers=tech).json()
        self.assertEqual(current["status"],"QUEUED")
        self.assertFalse(any(x["event_type"].startswith("TECHNICAL_FINDING") for x in current["timeline"]))

    def test_demo_seed_reset_is_deterministic_and_preserves_non_demo_and_weather(self):
        official={"station_id":"maitri","observation_time":"2026-10-04T03:00:00+00:00","temperature_c":-15.1,"pressure_hpa":1001.0,"relative_humidity_pct":70.0,"wind_speed":5.0,"wind_speed_unit":"m/s","wind_direction_deg":180.0,"source":"NCPOR","source_type":"NCPOR_OFFICIAL_OBSERVATION","quality":"GOOD","fetched_at":"2026-10-04T03:01:00+00:00"}
        with patch.object(main.NCPOR_PROVIDER,"fetch_station_observation",return_value=official):
            self.client.post("/api/weather/official/maitri/refresh",headers=self.hq)
        regular=self.client.post("/api/operations",headers={**self.hq,"Idempotency-Key":"non-demo-preserved"},json={"operation_type":"SCENARIO_FOLLOWUP","title":"Persisted user operation","base_revision":0}).json()
        status=self.client.get("/api/demo/status",headers=self.hq)
        self.assertEqual(status.json()["status"],"ABSENT")
        first=self.client.post("/api/demo/seed",headers=self.hq)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json()["status"],"SEEDED")
        self.assertEqual(first.json()["records"]["SCENARIO"],["SC-DEMO-001"])
        self.assertEqual(first.json()["records"]["OPERATION"],["OP-DEMO-001"])
        self.assertEqual(first.json()["records"]["OBSERVATION"],["OBS-DEMO-001","OBS-DEMO-002"])
        snapshot=self.client.get("/api/hq/command-center",headers=self.hq).json()
        self.assertEqual(snapshot["weather"]["source_type"],"NCPOR_OFFICIAL_OBSERVATION")
        self.assertEqual(snapshot["operations"][0]["status"],"CLOSED")
        tech_login=self.client.post("/auth/login",json={"username":"admin_tech","password":"123","station":"maitri","role":"technical"}).json()
        context=self.client.get("/api/technical/context/OP-DEMO-001",headers={"Authorization":f"Bearer {tech_login['token']}"}).json()
        self.assertEqual(context["technical_findings"][0]["id"],"TF-DEMO-001")
        self.assertEqual(context["technical_findings"][0]["status"],"COMPLETE")
        forbidden=self.client.post("/api/demo/reset",headers=self.station)
        self.assertEqual(forbidden.status_code,403)
        reset=self.client.post("/api/demo/reset",headers=self.hq)
        self.assertEqual(reset.status_code,200,reset.text)
        self.assertEqual(reset.json()["status"],"ABSENT")
        self.assertEqual(self.client.get("/api/demo/status",headers=self.hq).json()["status"],"ABSENT")
        self.assertEqual(self.client.get("/api/operations",headers=self.hq).json()["operations"][0]["id"],regular["id"])
        self.assertEqual(self.client.get("/api/weather/official/maitri",headers=self.hq).json()["observation"]["temperature_c"],-15.1)
        second=self.client.post("/api/demo/seed",headers=self.hq)
        self.assertEqual(second.status_code,200,second.text)
        self.assertEqual(second.json(),first.json())
        reset_again=self.client.post("/api/demo/reset",headers=self.hq)
        self.assertEqual(reset_again.status_code,200,reset_again.text)

    def test_weather_refresh_cache_and_local_history_are_separate(self):
        observation = {"station_id": "maitri", "observation_time": "2026-10-01T12:00:00+00:00", "temperature_c": -15.0, "pressure_hpa": 958.0, "relative_humidity_pct": 40.0, "wind_speed": 6.0, "wind_speed_unit": "m/s", "wind_direction_deg": 170.0, "source": "NCPOR", "source_type": "NCPOR_OFFICIAL_OBSERVATION", "quality": "GOOD", "fetched_at": "2026-10-01T12:01:00+00:00"}
        with patch.object(main.NCPOR_PROVIDER, "fetch_station_observation", return_value=observation):
            fetched = self.client.post("/api/weather/official/maitri/refresh", headers=self.hq)
        self.assertEqual(fetched.json()["status"], "AVAILABLE")
        self.assertEqual(fetched.json()["observation"]["source_type"], "NCPOR_OFFICIAL_OBSERVATION")
        with patch.object(main.NCPOR_PROVIDER, "fetch_station_observation", side_effect=RuntimeError("offline")):
            stale = self.client.post("/api/weather/official/maitri/refresh", headers=self.hq)
        self.assertEqual(stale.json()["status"], "LAST_NCPOR_OBSERVATION")
        self.assertEqual(stale.json()["observation"]["temperature_c"], -15.0)
        history = self.client.get("/api/weather/historical/maitri", headers=self.hq).json()
        self.assertEqual(history["source_type"], "NCPOR_HISTORICAL")
        self.assertNotEqual(history["source_type"], fetched.json()["observation"]["source_type"])

    def test_ncpor_adapter_normalizes_public_station_page(self):
        from datetime import datetime, timezone

        stamp = int(datetime(2026, 10, 2, 17, 0, tzinfo=timezone.utc).timestamp() * 1000)
        live = (f'''<script>name: "Temperature", dataPoints: [{{ x: {stamp}, y: -15.2 }}]
        name: "Wind Speed", dataPoints: [{{ x: {stamp}, y: 6.1 }}]
        name: "Air Pressure", dataPoints: [{{ x: {stamp}, y: 958.2 }}]
        name: "Relative Humidity", dataPoints: [{{ x: {stamp}, y: 41.0 }}]</script>''').encode()
        wind = b'<td>Wind Direction: 172\xc2\xb0 S</td>'

        class Response:
            status = 200
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def read(self, _limit): return self.payload

        responses = iter((live, wind))
        provider = NCPOROfficialProvider(opener=lambda *_args, **_kwargs: Response(next(responses)))
        normalized = provider.fetch_station_observation("maitri")
        self.assertEqual(normalized["source_type"], "NCPOR_OFFICIAL_OBSERVATION")
        self.assertEqual(normalized["temperature_c"], -15.2)
        self.assertEqual(normalized["wind_direction_deg"], 172.0)
        self.assertEqual(normalized["observation_time"], "2026-10-02T17:00:00+00:00")

    def test_local_monthly_weather_can_be_explicitly_linked_to_scenario(self):
        history = self.client.get("/api/weather/historical/maitri", headers=self.hq).json()["observations"]
        selected = history[0]
        self.assertEqual(selected["source_type"], "NCPOR_HISTORICAL")
        self.assertEqual(selected["resolution"], "MONTHLY_MEAN")
        self.assertIn("loaded_at", selected)
        self.assertNotIn("fetched_at", selected)
        self.assertEqual(history[-1]["observation_time"], "2015-12")
        scenario = self.client.post("/hq/scenarios/maitri", headers=self.hq, json={
            "name": "Historical month replay", "base_revision": 0, "model_version": "v0.2.0-causal-accounting",
            "parameters": {"chp_available": True, "fuel_consumption_lph": 9.5, "critical_load_kw": 100, "deferrable_load_kw": 20, "outdoor_temperature_c": selected["temperature_c"], "water_demand_l_per_day": 1000, "duration_hours": 24},
            "weather_reference": selected,
        })
        self.assertEqual(scenario.status_code, 200, scenario.text)
        self.assertEqual(scenario.json()["weather_reference"]["source_type"], "NCPOR_HISTORICAL")
        restored = self.client.get(f"/hq/scenarios/maitri/{scenario.json()['id']}", headers=self.hq).json()
        self.assertEqual(restored["weather_reference"]["id"], selected["id"])

    def test_scenario_model_is_repeatable_and_database_integrity_holds(self):
        payload={"name":"Repeatable CHP case","base_revision":0,"model_version":"v0.2.0-causal-accounting","parameters":{"chp_available":False,"fuel_consumption_lph":9.5,"critical_load_kw":100,"deferrable_load_kw":20,"outdoor_temperature_c":-24,"water_demand_l_per_day":1000,"duration_hours":24}}
        first=self.client.post("/hq/scenarios/maitri",headers=self.hq,json=payload)
        second=self.client.post("/hq/scenarios/maitri",headers=self.hq,json=payload)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(second.status_code,200,second.text)
        self.assertEqual(first.json()["result"],second.json()["result"])
        with main.database() as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(),[])

    def test_idempotent_operation_lifecycle_and_delivery_semantics(self):
        body = {"operation_type": "MEASUREMENT_REQUEST", "title": "Indoor temperature reading", "description": "Check reading after scenario review", "priority": "P1", "assigned_role": "station", "base_revision": 0, "metadata": {"metric": "Indoor temperature", "unit": "°C"}}
        headers = {**self.hq, "Idempotency-Key": "test-measurement-001"}
        first = self.client.post("/api/operations", json=body, headers=headers)
        replay = self.client.post("/api/operations", json=body, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["id"], replay.json()["id"])
        self.assertTrue(replay.json()["idempotent_replay"])
        operation_id = first.json()["id"]
        self.assertEqual(first.json()["sync_state"], "PENDING_SYNC")
        reviewed = self.client.post(f"/api/operations/{operation_id}/review", headers=self.hq, json={"note": "Reviewed"})
        self.assertEqual(reviewed.json()["status"], "REVIEWED_FOR_COMMUNICATION")
        queued = self.client.post(f"/api/operations/{operation_id}/queue", headers=self.hq)
        self.assertEqual(queued.json()["status"], "QUEUED")
        self.assertEqual(queued.json()["sync_state"], "PENDING_SYNC")
        with main.database() as connection:
            main.record_operation_delivery(connection, operation_id, metadata={"transport_receipt": "test-confirmed"})
        seen = self.client.post(f"/api/operations/{operation_id}/seen", headers=self.station)
        accepted = self.client.post(f"/api/operations/{operation_id}/accept", headers=self.station, json={"note": "Acknowledged"})
        self.assertEqual(seen.json()["status"], "SEEN")
        self.assertEqual(accepted.json()["status"], "ACCEPTED")
        report = self.client.post(f"/api/operations/{operation_id}/report-complete", headers=self.station, json={"observations": {"temperature": 18.2}, "note": "Reading recorded"})
        self.assertEqual(report.json()["status"], "REPORTED_COMPLETE")
        reviewed_outcome = self.client.post(f"/api/operations/{operation_id}/review", headers=self.hq, json={"note": "Reviewed"})
        self.assertIn("not claim independent verification", reviewed_outcome.json()["review_semantics"])
        closed = self.client.post(f"/api/operations/{operation_id}/close", headers=self.hq, json={"note": "Closed after review"})
        self.assertEqual(closed.json()["status"], "CLOSED")
        self.assertEqual(len(self.client.get(f"/api/operations/{operation_id}/timeline", headers=self.hq).json()), 9)
        self.assertFalse(any(route.path.endswith("/delivered") for route in main.app.routes))

    def test_station_scope_and_stale_revision_rejection(self):
        body = {"operation_type": "SCENARIO_FOLLOWUP", "title": "Follow up", "description": "Inspect a reported condition", "base_revision": 0}
        created = self.client.post("/api/operations", json=body, headers={**self.hq, "Idempotency-Key": "stale-test-001"})
        operation_id = created.json()["id"]
        with main.database() as connection:
            connection.execute("INSERT INTO station_events (station,event_type,source,occurred_at,payload,revision_hash,model_version,manifest_version,evidence_refs) VALUES ('maitri','TEST_REVISION','TEST','2026-10-01T00:00:00+00:00','{}','test','v0.1','v1','[]')")
        conflict = self.client.post(f"/api/operations/{operation_id}/review", headers=self.hq, json={"note": "Review"})
        self.assertEqual(conflict.status_code, 409)
        bharati = self.client.post("/auth/login", json={"username": "admin_hq", "password": "123", "station": "bharati", "role": "hq"}).json()
        denied = self.client.get(f"/api/operations/{operation_id}", headers={"Authorization": f"Bearer {bharati['token']}"})
        self.assertEqual(denied.status_code, 404)
        technical_only = self.client.post("/api/operations", headers={**self.hq, "Idempotency-Key": "tech-only-001"}, json={"operation_type":"SCENARIO_FOLLOWUP","title":"Technical inspection","base_revision":1,"assigned_role":"technical"})
        excluded = self.client.get(f"/api/operations/{technical_only.json()['id']}", headers=self.station)
        self.assertEqual(excluded.status_code, 403)

    def test_station_manual_observation_revision_ack_and_idempotent_sync(self):
        created = self.client.post("/api/operations", headers={**self.hq, "Idempotency-Key": "station-sync-test"}, json={"operation_type":"INSPECTION_REQUEST","title":"Inspect thermal plant","description":"Report availability","base_revision":0,"metadata":{"asset":"Thermal plant","requested_checks":"Availability"}})
        operation_id = created.json()["id"]
        self.client.post(f"/api/operations/{operation_id}/queue", headers=self.hq)
        with main.database() as connection:
            main.record_operation_delivery(connection, operation_id, metadata={"receipt":"test"})
        self.assertEqual(self.client.get("/api/operations", headers=self.station).json()["operations"][0]["status"], "DELIVERED")
        observation = {"metric":"Indoor Temperature","value":"-4.2","unit":"°C","observation_time":"2026-10-03T10:42:00+05:30","method":"Manual station observation","quality":"UNASSESSED","confidence":"MEDIUM","notes":"Read from panel."}
        response = self.client.post(f"/api/station/operations/{operation_id}/observations", headers=self.station, json=observation)
        self.assertEqual(response.json()["source_type"], "MANUAL")
        self.assertEqual(self.client.get("/api/station/observations", headers=self.station).json()["observations"][0]["metric"], "Indoor Temperature")
        key_headers = {**self.station, "Idempotency-Key":"station-accept-event-1"}
        event = {"operation_id":operation_id,"action":"accept","payload":{"note":"Accepted"}}
        accepted = self.client.post("/api/station/sync", headers=key_headers, json=event)
        replay = self.client.post("/api/station/sync", headers=key_headers, json=event)
        self.assertEqual(accepted.json()["status"], "ACCEPTED")
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(len(replay.json()["timeline"]), len(accepted.json()["timeline"]))
        with main.database() as connection:
            connection.execute("INSERT INTO station_events (station,event_type,source,occurred_at,payload,revision_hash,model_version,manifest_version,evidence_refs) VALUES ('maitri','TEST_STATE_CHANGE','TEST','2026-10-03T00:00:00+00:00','{}','test','v0.1','v1','[]')")
        blocked = self.client.post(f"/api/operations/{operation_id}/report-complete", headers=self.station, json={"observations":{"summary":"Inspected"}})
        self.assertEqual(blocked.status_code, 409)
        complete = self.client.post(f"/api/operations/{operation_id}/report-complete", headers=self.station, json={"observations":{"summary":"Inspected"},"acknowledge_revision":True})
        self.assertEqual(complete.json()["status"], "REPORTED_COMPLETE")
        review = self.client.post(f"/api/operations/{operation_id}/review", headers=self.hq, json={"classification":"CONSISTENT","notes":"Station report reviewed."})
        self.assertEqual(review.json().get("outcome_review",{}).get("classification"), "CONSISTENT", review.text)
        self.assertIn("not claim independent verification", review.json()["review_semantics"])

    def test_batch_sync_idempotency_partial_results_and_revision_conflict(self):
        created=self.client.post("/api/operations",headers={**self.hq,"Idempotency-Key":"batch-flow-op"},json={"operation_type":"SCENARIO_FOLLOWUP","title":"Batch test","base_revision":0})
        op_id=created.json()["id"]
        self.client.post(f"/api/operations/{op_id}/queue",headers=self.hq)
        with main.database() as connection: main.record_operation_delivery(connection,op_id,metadata={"receipt":"test"})
        import hashlib, json
        def event(event_id,kind,created_at,revision=0):
            body={"note":kind}
            canonical=json.dumps({"action":kind,"operation_id":op_id,"payload":body},sort_keys=True,separators=(",",":"))
            return {"event_id":event_id,"idempotency_key":f"dev-{event_id}","operation_id":op_id,"event_type":kind,"created_at":created_at,"local_revision":revision,"payload":body,"payload_hash":hashlib.sha256(canonical.encode()).hexdigest()}
        batch={"station_id":"maitri","device_id":"dev-test","base_server_revision":0,"events":[event("evt-a","accept","2026-10-03T10:00:00Z"),event("evt-b","progress","2026-10-03T10:01:00Z")]}
        first=self.client.post("/api/sync/batch",headers=self.station,json=batch)
        self.assertEqual(len(first.json()["accepted"]),2,first.text)
        replay=self.client.post("/api/sync/batch",headers=self.station,json=batch)
        self.assertEqual(len(replay.json()["already_applied"]),2,replay.text)
        observation_payload={"metric":"Fuel Level","value":"42","unit":"%","observation_time":"2026-10-03T10:02:00Z","method":"Manual station check","quality":"UNASSESSED","confidence":"MEDIUM","notes":"Visible gauge."}
        conflict_event={"event_id":"evt-conflict","idempotency_key":"dev-evt-conflict","operation_id":op_id,"event_type":"observation","created_at":"2026-10-03T10:02:00Z","local_revision":9,"payload":observation_payload}
        conflict_batch={"station_id":"maitri","device_id":"dev-test","base_server_revision":0,"events":[conflict_event]}
        conflict=self.client.post("/api/sync/batch",headers=self.station,json=conflict_batch)
        self.assertEqual(conflict.json()["conflicts"][0]["status"],"REVISION_CONFLICT")
        self.assertEqual(len(self.client.get("/api/sync/conflicts",headers=self.station).json()["conflicts"]),1)
        conflict_id=conflict.json()["conflicts"][0]["conflict_id"]
        resolved=self.client.post(f"/api/sync/conflicts/{conflict_id}/resolve",headers=self.station,json={"resolution":"APPLY_LOCAL_EVENT","note":"Reviewed current station revision."})
        replacement=resolved.json()["replacement_event"]
        replacement_body=replacement["payload"]
        retry={"station_id":"maitri","device_id":"dev-test","base_server_revision":0,"events":[{"event_id":replacement["event_id"],"idempotency_key":replacement["idempotency_key"],"operation_id":replacement["operation_id"],"event_type":replacement["event_type"],"created_at":replacement["created_at"],"local_revision":replacement["local_revision"],"payload":replacement_body["payload"],"payload_hash":replacement["payload_hash"]}]}
        retried=self.client.post("/api/sync/batch",headers=self.station,json=retry)
        self.assertEqual(len(retried.json()["accepted"]),1,retried.text)
        status=self.client.get("/api/sync/status",headers=self.station).json()
        self.assertEqual(status["counts"]["SYNCED"],3)

    def test_station_inbox_confirms_real_http_delivery_of_queued_operations(self):
        op=self.client.post("/api/operations",headers={**self.hq,"Idempotency-Key":"http-delivery-test"},json={"operation_type":"SCENARIO_FOLLOWUP","title":"Delivery receipt test","base_revision":0}).json()
        queued=self.client.post(f"/api/operations/{op['id']}/queue",headers=self.hq)
        self.assertEqual(queued.json()["status"],"QUEUED")
        inbox=self.client.get("/api/operations",headers=self.station).json()
        self.assertEqual(inbox["operations"][0]["status"],"QUEUED")
        received=self.client.post(f"/api/station/operations/{op['id']}/receive",headers=self.station)
        self.assertEqual(received.json()["status"],"DELIVERED")
        replay=self.client.post(f"/api/station/operations/{op['id']}/receive",headers=self.station)
        self.assertEqual(replay.json()["status"],"DELIVERED")
        events=[e for e in replay.json()["timeline"] if e["event_type"]=="OPERATION_DELIVERED"]
        self.assertEqual(len(events),1)

    def test_batch_partial_failure_blocks_dependent_event_until_predecessor_arrives(self):
        op=self.client.post("/api/operations",headers={**self.hq,"Idempotency-Key":"predecessor-test"},json={"operation_type":"SCENARIO_FOLLOWUP","title":"Causal ordering test","base_revision":0}).json()
        self.client.post(f"/api/operations/{op['id']}/queue",headers=self.hq)
        self.client.post(f"/api/station/operations/{op['id']}/receive",headers=self.station)
        import hashlib,json
        def item(eid,kind,stamp):
            payload={"note":kind};body=json.dumps({"action":kind,"operation_id":op["id"],"payload":payload},sort_keys=True,separators=(",",":"),ensure_ascii=False)
            return {"event_id":eid,"idempotency_key":f"dev-{eid}","operation_id":op["id"],"event_type":kind,"created_at":stamp,"local_revision":0,"payload":payload,"payload_hash":hashlib.sha256(body.encode()).hexdigest()}
        batch={"station_id":"maitri","device_id":"dev-test","base_server_revision":0,"events":[item("progress-before-accept","progress","2026-10-03T12:00:00Z"),item("accept-after-progress","accept","2026-10-03T12:01:00Z")]}
        first=self.client.post("/api/sync/batch",headers=self.station,json=batch).json()
        self.assertEqual(first["conflicts"][0]["status"],"BLOCKED_PENDING_PREDECESSOR")
        self.assertEqual(first["accepted"][0]["event_id"],"accept-after-progress")
        retry=self.client.post("/api/sync/batch",headers=self.station,json={**batch,"events":[batch["events"][0]]}).json()
        self.assertEqual(retry["accepted"][0]["event_id"],"progress-before-accept")

    def test_reconciliation_uses_scenario_and_report_snapshots_and_reopens(self):
        scenario=self.client.post("/hq/scenarios/maitri",headers=self.hq,json={"name":"CHP unavailable case","base_revision":0,"model_version":"v0.2.0-causal-accounting","parameters":{"chp_available":False,"fuel_consumption_lph":9.5,"critical_load_kw":100,"deferrable_load_kw":20,"outdoor_temperature_c":-24,"water_demand_l_per_day":1000,"duration_hours":24}})
        self.assertEqual(scenario.status_code,200,scenario.text)
        operation=self.client.post("/api/operations",headers={**self.hq,"Idempotency-Key":"recon-op"},json={"operation_type":"INSPECTION_REQUEST","title":"Inspect CHP","description":"Check CHP and indoor temperature","base_revision":0,"scenario_id":scenario.json()["id"],"model_version":"v0.2.0-causal-accounting","metadata":{"asset":"CHP","requested_checks":"Availability"}}).json()
        op_id=operation["id"]
        self.client.post(f"/api/operations/{op_id}/queue",headers=self.hq)
        with main.database() as connection: main.record_operation_delivery(connection,op_id,metadata={"receipt":"test"})
        self.client.post(f"/api/operations/{op_id}/accept",headers=self.station,json={"note":"accepted"})
        self.client.post(f"/api/operations/{op_id}/progress",headers=self.station,json={"note":"started"})
        compare_at=datetime.now(timezone.utc).isoformat()
        manual={"metric":"Indoor Temperature","value":"-4.2","unit":"°C","observation_time":compare_at,"method":"Manual","quality":"GOOD","confidence":"MEDIUM","notes":"Panel reading"}
        saved=self.client.post(f"/api/station/operations/{op_id}/observations",headers=self.station,json=manual)
        self.assertEqual(saved.json()["source_type"],"MANUAL")
        chp=self.client.post(f"/api/station/operations/{op_id}/observations",headers=self.station,json={"metric":"CHP Availability","value":"Available","unit":"state","observation_time":compare_at,"method":"Manual inspection","quality":"GOOD","confidence":"MEDIUM","notes":"Operator inspected CHP."})
        self.assertEqual(chp.json()["source_type"],"MANUAL")
        report=self.client.post(f"/api/operations/{op_id}/report-complete",headers=self.station,json={"observations":{"summary":"Inspected"}})
        self.assertEqual(report.json()["status"],"REPORTED_COMPLETE")
        rec=self.client.post(f"/api/reconciliation/{op_id}/create",headers=self.hq,json={})
        self.assertEqual(rec.status_code,200,rec.text)
        direct=next(x for x in rec.json()["comparison_result"]["items"] if x["metric"]=="Indoor Temperature")
        self.assertEqual(direct["type"],"NON_COMPARABLE")
        self.assertIn("Projection is missing",direct["reason"])
        state=next(x for x in rec.json()["comparison_result"]["items"] if x["metric"]=="CHP Availability")
        self.assertEqual(state["type"],"STATE_COMPARISON")
        self.assertEqual(state["scenario_assumption"],"Unavailable")
        self.assertEqual(rec.json()["projection_snapshot"]["model_version"],"v0.2.0-causal-accounting")
        review=self.client.post(f"/api/reconciliation/{rec.json()['id']}/review",headers=self.hq,json={"classification":"DIFFERS_FROM_PROJECTION","notes":"Observed value differs from stored projection.","follow_up_action":"Request a second manual indoor temperature reading."})
        self.assertEqual(review.json()["status"],"REVIEWED")
        self.assertEqual(review.json()["review_classification"],"DIFFERS_FROM_PROJECTION")
        followup=self.client.post(f"/api/reconciliation/{rec.json()['id']}/follow-up",headers={**self.hq,"Idempotency-Key":"recon-follow-up"},json={"operation_type":"SCENARIO_FOLLOWUP","title":"Repeat temperature reading","description":"Request a second manual indoor temperature reading."})
        self.assertEqual(followup.status_code,200,followup.text)
        self.assertEqual(followup.json()["metadata"]["original_reconciliation_id"],rec.json()["id"])
        self.client.post(f"/api/station/operations/{op_id}/observations",headers=self.station,json={"metric":"Indoor Temperature","value":"-5.5","unit":"°C","observation_time":"2026-10-03T11:10:00+05:30","method":"Manual follow-up reading","quality":"UNASSESSED","confidence":"MEDIUM","notes":"New information received after review."})
        reopened=self.client.post(f"/api/reconciliation/{rec.json()['id']}/reopen",headers=self.hq,json={"note":"Additional evidence received"})
        self.assertEqual(reopened.json()["status"],"REOPENED")
        self.assertEqual(len(reopened.json()["observation_snapshot"]["observations"]),3)
        original_snapshot=reopened.json()["events"][0]["payload"]["observation_snapshot"]
        self.assertEqual(len(original_snapshot["observations"]),2)
        second_review=self.client.post(f"/api/reconciliation/{rec.json()['id']}/review",headers=self.hq,json={"classification":"INSUFFICIENT_EVIDENCE","notes":"Reviewing the added observation."})
        self.assertEqual(second_review.json()["status"],"REVIEWED")
        self.assertEqual([e["event_type"] for e in second_review.json()["events"]], ["RECONCILIATION_CREATED","RECONCILIATION_REVIEWED","FOLLOW_UP_OPERATION_CREATED","RECONCILIATION_REOPENED","RECONCILIATION_REVIEWED"])


if __name__ == "__main__":
    unittest.main()


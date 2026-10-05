import json
import os
import tempfile
import urllib.error
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import edge_service, main
from backend.emulator.process_model import state_at
from backend.model.scenario_model import MODEL_VERSION, run_scenario


class CausalModelTests(unittest.TestCase):
    def setUp(self):
        self.baseline={
            "power_available":{"value":210,"unit":"kW"},"critical_load":{"value":128,"unit":"kW"},
            "deferrable_load":{"value":31,"unit":"kW"},"fuel_reserve":{"value":51000,"unit":"L"},
            "water_production":{"value":92,"unit":"L/h"},"water_reserve":{"value":7100,"unit":"L"},
        }
        self.parameters={"chp_available":True,"fuel_consumption_lph":10,"fuel_inflow_lph":1,"critical_load_kw":128,
            "deferrable_load_kw":31,"water_demand_l_per_day":1000,"duration_hours":24}

    def test_fuel_water_and_power_accounting_equations(self):
        result=run_scenario(self.baseline,self.parameters)
        p=result["projected"]
        self.assertEqual(p["fuel_l"],51000+24-240)
        self.assertEqual(p["water_l"],7100+92*24-1000)
        self.assertEqual(p["power_balance_kw"],210-128-31)
        self.assertEqual(p["power_deficit_kw"],0)
        self.assertIsNone(p["thermal_balance_kwth"])
        self.assertEqual(result["model_version"],MODEL_VERSION)

    def test_unavailable_chp_and_resource_shortages_are_visible(self):
        params={**self.parameters,"chp_available":False,"fuel_consumption_lph":1000,"duration_hours":72,"water_demand_l_per_day":5000}
        result=run_scenario(self.baseline,params)
        p=result["projected"]
        self.assertEqual(p["power_available_kw"],0)
        self.assertEqual(p["water_production_lph"],0)
        self.assertEqual(p["fuel_shortage_l"],20928)
        self.assertGreater(p["water_shortage_l"],0)
        codes={x["code"]:x["status"] for x in result["constraints"]}
        self.assertEqual(codes["THERMAL_DEFICIT"],"UNKNOWN")

    def test_scenario_run_does_not_mutate_baseline_or_inputs(self):
        before=json.dumps((self.baseline,self.parameters),sort_keys=True)
        run_scenario(self.baseline,self.parameters)
        self.assertEqual(json.dumps((self.baseline,self.parameters),sort_keys=True),before)


class EmulatorTests(unittest.TestCase):
    def test_emulator_is_repeatable_and_faulted_observation_is_separate(self):
        a=state_at("maitri",4,seed=77,fault_at=4)
        b=state_at("maitri",4,seed=77,fault_at=4)
        normal=state_at("maitri",3,seed=77,fault_at=4)
        self.assertEqual(a,b)
        self.assertTrue(a["fault_injected"])
        self.assertEqual(a["state"]["power_available"]["value"],0)
        self.assertGreater(normal["state"]["power_available"]["value"],0)
        self.assertEqual(a["state"]["power_available"]["source_type"],"DEMO_SYNTHETIC")


class ProjectionCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.created=datetime.now(timezone.utc)
        self.scenario={"id":"SC-T","station":"maitri","model_version":MODEL_VERSION,"created_at":self.created.isoformat(),
            "inputs":{"duration_hours":24,"chp_available":False},"result":{"model_version":MODEL_VERSION,"projected":{"fuel_l":100}}}

    def observation(self,**overrides):
        value={"metric":"Fuel level","value":"90","unit":"L","station_id":"maitri","quality":"GOOD","observation_time":(self.created+timedelta(hours=2)).isoformat()}
        return {**value,**overrides}

    def test_compatible_quality_time_unit_station_and_model(self):
        result=main.build_projection_comparison(self.scenario,[self.observation()],MODEL_VERSION)
        self.assertEqual(result["status"],"COMPARABLE")
        self.assertEqual(result["items"][0]["uncertainty_status"],"NOT_MODELED")

    def test_comparison_rejects_bad_quality_time_window_station_and_versions(self):
        cases=[
            (self.observation(quality="UNASSESSED"),"quality"),
            (self.observation(observation_time=(self.created-timedelta(minutes=1)).isoformat()),"timestamp"),
            (self.observation(station_id="bharati"),"different stations"),
            (self.observation(unit="gallons"),"units"),
        ]
        for obs,reason in cases:
            with self.subTest(reason=reason):
                result=main.build_projection_comparison(self.scenario,[obs],MODEL_VERSION)
                self.assertEqual(result["status"],"NOT_COMPARABLE")
                self.assertIn(reason,result["items"][0]["reason"])
        result=main.build_projection_comparison(self.scenario,[self.observation()],"old-model")
        self.assertEqual(result["status"],"NOT_COMPARABLE")
        self.assertIn("model versions",result["reason"])


class EdgePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old_path=edge_service.DB_PATH
        edge_service.DB_PATH=Path(self.temp.name)/"edge.sqlite"
        self.client_context=TestClient(edge_service.app)
        self.client=self.client_context.__enter__()
        self.headers={"Authorization":"Bearer local-edge-token"}

    def tearDown(self):
        self.client_context.__exit__(None,None,None)
        edge_service.DB_PATH=self.old_path
        self.temp.cleanup()

    def test_local_observation_and_outbox_survive_edge_restart(self):
        saved=self.client.post("/api/edge/observations?station_id=maitri",headers=self.headers,json={
            "operation_id":"OP-LOCAL","metric":"CHP Availability","value":"Unavailable","unit":"state",
            "observation_time":"2026-10-04T12:00:00Z","method":"Manual inspection","quality":"GOOD"})
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertTrue(saved.json()["stored_locally"])
        self.client_context.__exit__(None,None,None)
        self.client_context=TestClient(edge_service.app)
        self.client=self.client_context.__enter__()
        queue=self.client.get("/api/edge/outbox?station_id=maitri",headers=self.headers)
        self.assertEqual(queue.status_code,200,queue.text)
        self.assertEqual(len(queue.json()["events"]),1)
        self.assertEqual(queue.json()["events"][0]["sync_status"],"PENDING")


class HQTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old_db=main.DB_PATH
        main.DB_PATH=Path(self.temp.name)/"hq.sqlite"
        self.old_mode=os.environ.get("PRATIBIMB_DEMO_MODE")
        self.old_token=os.environ.get("PRATIBIMB_GATEWAY_TOKEN")
        os.environ["PRATIBIMB_DEMO_MODE"]="1"
        os.environ["PRATIBIMB_GATEWAY_TOKEN"]="test-gateway-token"
        self.context=TestClient(main.app)
        self.client=self.context.__enter__()
        login=self.client.post("/auth/login",json={"username":"admin_hq","password":"123","station":"maitri","role":"hq"}).json()
        self.headers={"Authorization":f"Bearer {login['token']}"}

    def tearDown(self):
        self.context.__exit__(None,None,None)
        main.DB_PATH=self.old_db
        if self.old_mode is None:os.environ.pop("PRATIBIMB_DEMO_MODE",None)
        else:os.environ["PRATIBIMB_DEMO_MODE"]=self.old_mode
        if self.old_token is None:os.environ.pop("PRATIBIMB_GATEWAY_TOKEN",None)
        else:os.environ["PRATIBIMB_GATEWAY_TOKEN"]=self.old_token
        self.temp.cleanup()

    def test_offline_gateway_rejects_transport_then_recovers_and_persists_state(self):
        stopped=self.client.post("/api/gateway/control",headers=self.headers,json={"status":"OFFLINE"})
        self.assertEqual(stopped.status_code,200,stopped.text)
        blocked=self.client.get("/api/gateway/outbox/maitri",headers={"X-Sync-Token":"test-gateway-token"})
        self.assertEqual(blocked.status_code,503)
        restored=self.client.post("/api/gateway/control",headers=self.headers,json={"status":"CONNECTED"})
        self.assertEqual(restored.status_code,200)
        status=self.client.get("/api/gateway/status",headers=self.headers).json()
        self.assertEqual(status["status"],"CONNECTED")

    def test_real_edge_workflow_remains_local_while_offline_then_syncs_once(self):
        operation=self.client.post("/api/operations",headers={**self.headers,"Idempotency-Key":"independent-edge-op"},json={
            "operation_type":"INSPECTION_REQUEST","title":"Edge integration inspection","description":"Local work test",
            "priority":"P1","assigned_role":"station","base_revision":0,"metadata":{"asset":"CHP","requested_checks":"Availability"}})
        self.assertEqual(operation.status_code,200,operation.text)
        op_id=operation.json()["id"]
        self.client.post(f"/api/operations/{op_id}/review",headers=self.headers,json={"note":"Reviewed"})
        self.client.post(f"/api/operations/{op_id}/queue",headers=self.headers)

        edge_temp=tempfile.TemporaryDirectory()
        old_edge_path=edge_service.DB_PATH
        edge_service.DB_PATH=Path(edge_temp.name)/"independent-edge.sqlite"
        edge_context=TestClient(edge_service.app)
        edge=edge_context.__enter__()
        token="test-gateway-token"

        def bridge(path,body=None):
            if path=="/ingest":
                response=self.client.post("/api/gateway/ingest",headers={"X-Sync-Token":token},json=body)
            elif path.startswith("/outbox/"):
                station=path.rsplit("/",1)[1]
                response=self.client.get(f"/api/gateway/outbox/{station}",headers={"X-Sync-Token":token})
            elif path=="/ack":
                response=self.client.post("/api/gateway/ack",headers={"X-Sync-Token":token},json=body)
            else:
                raise AssertionError(path)
            if response.status_code >= 400:
                raise urllib.error.HTTPError(path,response.status_code,response.text,None,None)
            return response.json()

        edge_headers={"Authorization":"Bearer local-edge-token"}
        try:
            with patch.object(edge_service,"GATEWAY_TOKEN",token),patch.object(edge_service,"gateway_request",side_effect=bridge):
                first_sync=edge.post("/api/edge/sync",headers=edge_headers,json={"station_id":"maitri"})
                self.assertEqual(first_sync.status_code,200,first_sync.text)
                self.assertEqual(first_sync.json()["received_operation_ids"],[op_id])
                self.assertEqual(self.client.get(f"/api/operations/{op_id}",headers=self.headers).json()["status"],"DELIVERED")

                self.client.post("/api/gateway/control",headers=self.headers,json={"status":"OFFLINE"})
                accepted=edge.post(f"/api/edge/operations/{op_id}/accept?station_id=maitri",headers=edge_headers,json={"note":"Accepted while disconnected"})
                self.assertEqual(accepted.status_code,200,accepted.text)
                local=edge.get("/api/edge/outbox?station_id=maitri",headers=edge_headers).json()["events"]
                self.assertEqual(local[0]["sync_status"],"PENDING")
                hidden=self.client.get(f"/api/operations/{op_id}",headers=self.headers).json()
                self.assertEqual(hidden["status"],"DELIVERED")
                self.assertFalse(any(event["event_type"]=="accept" for event in self.client.get("/api/gateway/events",headers=self.headers).json()["events"]))
                blocked=edge.post("/api/edge/sync",headers=edge_headers,json={"station_id":"maitri"})
                self.assertEqual(blocked.status_code,503)

                self.client.post("/api/gateway/control",headers=self.headers,json={"status":"CONNECTED"})
                synced=edge.post("/api/edge/sync",headers=edge_headers,json={"station_id":"maitri"})
                self.assertEqual(synced.status_code,200,synced.text)
                updated=self.client.get(f"/api/operations/{op_id}",headers=self.headers).json()
                self.assertEqual(updated["status"],"ACCEPTED")
                replay=edge.post("/api/edge/sync",headers=edge_headers,json={"station_id":"maitri"})
                self.assertEqual(replay.status_code,200,replay.text)
                refreshed=self.client.get(f"/api/operations/{op_id}",headers=self.headers).json()
                accepted_events=[event for event in refreshed["timeline"] if event["event_type"]=="OPERATION_ACCEPTED"]
                self.assertEqual(len(accepted_events),1)
        finally:
            edge_context.__exit__(None,None,None)
            edge_service.DB_PATH=old_edge_path
            edge_temp.cleanup()


if __name__=="__main__":
    unittest.main()

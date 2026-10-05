"""Standalone time-series process that writes synthetic state into station edge."""
import argparse
import json
import os
import time
import urllib.error
import urllib.request


def request(url: str, token: str, payload: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="GET" if data is None else "POST", headers={"X-Emulator-Token":token,"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=4) as response:
        return json.loads(response.read().decode())


def run(station: str, edge_url: str, token: str, seed: int, fault_at: int | None, interval: float, once: bool):
    status_url=f"{edge_url}/api/emulator/status?station_id={station}"
    try:
        current=request(status_url,token)
        tick=int(current.get("tick",0))
    except (urllib.error.URLError, TimeoutError, OSError):
        tick=0
    while True:
        tick += 1
        body={"station_id":station,"tick":tick,"seed":seed,"fault_at":fault_at}
        result=request(f"{edge_url}/api/edge/emulator/tick",token,body)
        print(f"EMULATOR {station.upper()} T+{tick} · {'CHP UNAVAILABLE' if result['fault_injected'] else 'NORMAL'} · edge rev {result['revision']}",flush=True)
        if once:
            return
        time.sleep(interval)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station",choices=("maitri","bharati"),default=os.environ.get("PRATIBIMB_EDGE_STATION","maitri"))
    parser.add_argument("--edge-url",default=os.environ.get("PRATIBIMB_EDGE_URL","http://127.0.0.1:8001"))
    parser.add_argument("--seed",type=int,default=int(os.environ.get("PRATIBIMB_EMULATOR_SEED","26060")))
    parser.add_argument("--fault-at",type=int,default=int(os.environ["PRATIBIMB_EMULATOR_FAULT_AT"]) if os.environ.get("PRATIBIMB_EMULATOR_FAULT_AT") else None)
    parser.add_argument("--interval",type=float,default=float(os.environ.get("PRATIBIMB_EMULATOR_INTERVAL","8")))
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args()
    run(args.station,args.edge_url,os.environ.get("PRATIBIMB_EMULATOR_TOKEN","local-emulator-token"),args.seed,args.fault_at,args.interval,args.once)


if __name__ == "__main__":
    main()

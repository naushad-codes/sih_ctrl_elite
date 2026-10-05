"""Launch PRATIBIMB's local HQ, station edge, emulator and Vite demo processes."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]


def wait_for(url: str, process: subprocess.Popen, label: str, timeout: float=25):
    deadline=time.time()+timeout
    while time.time()<deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{label} stopped during startup (exit {process.returncode})")
        try:
            with urllib.request.urlopen(url,timeout=1):
                return
        except Exception:
            time.sleep(0.35)
    raise RuntimeError(f"Timed out waiting for {label} at {url}")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hq-port",type=int,default=8000)
    parser.add_argument("--edge-port",type=int,default=8001)
    parser.add_argument("--web-port",type=int,default=5173)
    parser.add_argument("--station",choices=("maitri","bharati"),default="maitri")
    args=parser.parse_args()

    demo_dir=ROOT/"data"/"demo-runtime"
    demo_dir.mkdir(parents=True,exist_ok=True)
    env=os.environ.copy()
    env.update({
        "PYTHONPATH":str(ROOT),
        "PRATIBIMB_DB_PATH":str(demo_dir/"hq.sqlite"),
        "PRATIBIMB_EDGE_DB_PATH":str(demo_dir/"station-edge.sqlite"),
        "PRATIBIMB_GATEWAY_TOKEN":secrets.token_urlsafe(32),
        "PRATIBIMB_EDGE_TOKEN":secrets.token_urlsafe(24),
        "PRATIBIMB_EMULATOR_TOKEN":secrets.token_urlsafe(24),
        "PRATIBIMB_HQ_GATEWAY_URL":f"http://127.0.0.1:{args.hq_port}/api/gateway",
        "PRATIBIMB_EDGE_URL":f"http://127.0.0.1:{args.edge_port}",
        "PRATIBIMB_EDGE_STATION":args.station,
        "PRATIBIMB_DEMO_MODE":"1",
        "PRATIBIMB_CORS_ORIGINS":f"http://127.0.0.1:{args.web_port},http://localhost:{args.web_port}",
        "VITE_API_URL":f"http://127.0.0.1:{args.hq_port}",
        "VITE_EDGE_API_URL":f"http://127.0.0.1:{args.edge_port}",
        "VITE_EDGE_TOKEN":None,
    })
    env["VITE_EDGE_TOKEN"]=env["PRATIBIMB_EDGE_TOKEN"]
    services=[]
    commands=[
        ("HQ",[sys.executable,"-m","uvicorn","backend.main:app","--host","127.0.0.1","--port",str(args.hq_port)]),
        ("STATION EDGE",[sys.executable,"-m","uvicorn","backend.edge_service:app","--host","127.0.0.1","--port",str(args.edge_port)]),
    ]
    try:
        for label,command in commands:
            process=subprocess.Popen(command,cwd=ROOT,env=env)
            services.append((label,process))
        wait_for(f"http://127.0.0.1:{args.hq_port}/health",services[0][1],"HQ")
        wait_for(f"http://127.0.0.1:{args.edge_port}/health",services[1][1],"Station Edge")
        emulator=subprocess.Popen([sys.executable,"-m","backend.emulator.runner","--station",args.station],cwd=ROOT,env=env)
        services.append(("STATION EMULATOR",emulator))
        web_command=f"npm.cmd run dev -- --host 127.0.0.1 --port {args.web_port}" if os.name=="nt" else ["npm","run","dev","--","--host","127.0.0.1","--port",str(args.web_port)]
        web=subprocess.Popen(web_command,cwd=ROOT,env=env,shell=(os.name=="nt"))
        services.append(("FRONTEND",web))
        time.sleep(1.5)
        print("\nPRATIBIMB V2 LOCAL DEMO",flush=True)
        print(f"HQ service:       http://127.0.0.1:{args.hq_port}",flush=True)
        print(f"Station edge:     http://127.0.0.1:{args.edge_port}",flush=True)
        print(f"Frontend:         http://127.0.0.1:{args.web_port}",flush=True)
        print(f"Station profile:  {args.station.upper()}",flush=True)
        print("Demo credentials: admin_hq / 123 · admin_station / 123 · admin_tech / 123",flush=True)
        print("Transport:        CONNECTED · Stop Link in HQ Command Center to block sync",flush=True)
        print("Data stores:      data/demo-runtime/hq.sqlite · station-edge.sqlite",flush=True)
        print("Press Ctrl+C to stop all demo processes.\n",flush=True)
        while True:
            for label,process in services:
                if process.poll() is not None:
                    raise RuntimeError(f"{label} stopped unexpectedly (exit {process.returncode})")
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for _,process in reversed(services):
            if process.poll() is None:
                process.terminate()
        for _,process in reversed(services):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__=="__main__":
    main()

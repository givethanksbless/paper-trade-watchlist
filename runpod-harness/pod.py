#!/usr/bin/env python3
"""Controller for the private chat pod (Open WebUI + Ollama on one 24 GB GPU).

  python3 pod.py startcmd [--idle-min N] [--max-hours H]   print the pod start command (watchdog embedded)
  python3 pod.py launch   [--idle-min N] [--max-hours H]   create the pod - COSTS MONEY, asks first
  python3 pod.py list                                      list your pods
  python3 pod.py terminate POD_ID                          delete one pod
  python3 pod.py terminate-all                             delete every pod

launch/list/terminate need RUNPOD_API_KEY in the environment. startcmd needs nothing.
Safety net: besides watchdog.py, the start command runs a backup timer that deletes
the pod 10 min after the time cap even if the watchdog died.
"""
import argparse
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

IMAGE = "ghcr.io/open-webui/open-webui:ollama"
MODEL = "huihui_ai/deepseek-r1-abliterated:14b"
GPU_TYPES = ["NVIDIA RTX A5000", "NVIDIA GeForce RTX 3090", "NVIDIA GeForce RTX 4090"]
API = "https://rest.runpod.io/v1"


def start_cmd(max_hours):
    wd = base64.b64encode((Path(__file__).parent / "watchdog.py").read_bytes()).decode()
    backup = int(max_hours * 3600 + 600)
    script = (
        f"echo {wd} | base64 -d > /tmp/watchdog.py; "
        "nohup python3 /tmp/watchdog.py > /tmp/watchdog.log 2>&1 & "
        f"(sleep {backup}; curl -s -X DELETE -H \"Authorization: Bearer $RUNPOD_API_KEY\" "
        "https://rest.runpod.io/v1/pods/$RUNPOD_POD_ID) & "
        "cd /app/backend && exec bash start.sh"
    )
    return ["bash", "-c", script]


def pod_spec(idle_min, max_hours):
    return {
        "name": "private-chat",
        "imageName": IMAGE,
        "gpuTypeIds": GPU_TYPES,
        "gpuCount": 1,
        "cloudType": "COMMUNITY",
        "containerDiskInGb": 40,
        "volumeInGb": 0,
        "ports": ["8080/http"],
        "env": {"MODEL": MODEL, "IDLE_MIN": str(idle_min), "MAX_HOURS": str(max_hours)},
        "dockerStartCmd": start_cmd(max_hours),
    }


def api(method, path, body=None):
    key = os.environ.get("RUNPOD_API_KEY") or sys.exit("RUNPOD_API_KEY is not set")
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "runpod-harness/1.0"})  # default Python UA gets 403 from rest.runpod.io
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["startcmd", "launch", "list", "terminate", "terminate-all"])
    p.add_argument("pod_id", nargs="?")
    p.add_argument("--idle-min", type=float, default=30)
    p.add_argument("--max-hours", type=float, default=3)
    a = p.parse_args()

    if a.cmd == "startcmd":
        print(json.dumps(pod_spec(a.idle_min, a.max_hours), indent=2))
    elif a.cmd == "launch":
        if input("This starts a paid GPU pod. Type YES to continue: ") != "YES":
            sys.exit("cancelled")
        pod = api("POST", "/pods", pod_spec(a.idle_min, a.max_hours))
        print(f"pod {pod['id']} created - chat link (ready in ~5-10 min): "
              f"https://{pod['id']}-8080.proxy.runpod.net")
    elif a.cmd == "list":
        for pod in api("GET", "/pods") or []:
            print(pod["id"], pod.get("name"), pod.get("desiredStatus"), f"${pod.get('costPerHr')}/hr")
    elif a.cmd == "terminate":
        api("DELETE", f"/pods/{a.pod_id or sys.exit('give a POD_ID')}")
        print(f"terminated {a.pod_id}")
    elif a.cmd == "terminate-all":
        for pod in api("GET", "/pods") or []:
            api("DELETE", f"/pods/{pod['id']}")
            print(f"terminated {pod['id']}")


if __name__ == "__main__":
    main()

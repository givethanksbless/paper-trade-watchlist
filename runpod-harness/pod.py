#!/usr/bin/env python3
"""Controller for the private chat pod (Open WebUI + Ollama on one 48 GB GPU).

  python3 pod.py startcmd [--idle-min N] [--max-hours H]   print the pod start command (watchdog embedded)
  python3 pod.py launch   [--idle-min N] [--max-hours H]   create the pod - COSTS MONEY, asks first;
                                                           retries until a GPU is free, then runs guard
  python3 pod.py list                                      list your pods
  python3 pod.py terminate POD_ID                          delete one pod
  python3 pod.py terminate-all                             delete every pod
  python3 pod.py guard    [--max-hours H]                  watch from outside, delete stuck/overdue pods
  python3 pod.py collect                                   build memory/ from the village (read-only, free)

launch/list/terminate need RUNPOD_API_KEY in the environment. startcmd needs nothing.
Safety net: besides watchdog.py, the start command runs a backup timer that deletes
the pod 10 min after the time cap even if the watchdog died.
If profile.md sits next to this file, the pod gets a "private-chat" model that knows it.
With CHAT_EMAIL and CHAT_PASSWORD set, the chat gets that fixed login (sign-up off), and
launch feeds it a read-only copy of the village (collector.py -> feeder.py).
"""
import argparse
import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

IMAGE = "ghcr.io/open-webui/open-webui:ollama"
MODEL = "huihui_ai/qwen3.5-abliterated:27b-Claude-q8_0"  # 30 GB download
# 48 GB cards at ~$0.33-0.35/hr; Runpod takes whichever is free (pricier 48 GB cards left out on purpose)
GPU_TYPES = ["NVIDIA RTX A6000", "NVIDIA A40"]
EMBED_MODEL = "nomic-embed-text"  # small model that indexes the #Village library on the GPU
API = "https://rest.runpod.io/v1"
START_MIN = 15  # guard deletes a pod whose container hasn't started by then
LAUNCH_TRIES = 20  # one a minute while no GPU is free; failed tries cost nothing


def start_cmd(max_hours):
    here = Path(__file__).parent
    wd = base64.b64encode((here / "watchdog.py").read_bytes()).decode()
    profile = here / "profile.md"
    backup = int(max_hours * 3600 + 600)
    script = (
        f"echo {wd} | base64 -d > /tmp/watchdog.py; "
        + (f"echo {base64.b64encode(profile.read_bytes()).decode()} | base64 -d > /tmp/profile.md; "
           if profile.exists() else "") +
        "nohup python3 /tmp/watchdog.py > /tmp/watchdog.log 2>&1 & "
        f"(sleep {backup}; curl -s -X DELETE -H \"Authorization: Bearer $RUNPOD_API_KEY\" "
        "https://rest.runpod.io/v1/pods/$RUNPOD_POD_ID) & "
        "cd /app/backend && exec bash start.sh"
    )
    return ["bash", "-c", script]


def login():
    email, password = os.environ.get("CHAT_EMAIL"), os.environ.get("CHAT_PASSWORD")
    return (email, password) if email and password else None


def pod_spec(idle_min, max_hours):
    env = {"MODEL": MODEL, "IDLE_MIN": str(idle_min), "MAX_HOURS": str(max_hours),
           "DEFAULT_MODELS": "private-chat",  # Open WebUI preselects it in every chat
           "EMBED_MODEL": EMBED_MODEL, "RAG_EMBEDDING_ENGINE": "ollama", "RAG_EMBEDDING_MODEL": EMBED_MODEL}
    if login():  # Open WebUI creates this admin on first start and turns sign-up off
        env.update(WEBUI_ADMIN_EMAIL=login()[0], WEBUI_ADMIN_PASSWORD=login()[1], WEBUI_ADMIN_NAME="Jason")
    return {
        "name": "private-chat",
        "imageName": IMAGE,
        "gpuTypeIds": GPU_TYPES,
        "gpuCount": 1,
        "cloudType": "COMMUNITY",
        "containerDiskInGb": 80,
        "volumeInGb": 0,
        "ports": ["8080/http"],
        "env": env,
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


def pod_info(pod_id):
    # GraphQL has the runtime and GPU name, which the REST pod object leaves empty
    q = {"query": f'query {{ pod(input: {{podId: "{pod_id}"}}) '
                  '{ runtime { uptimeInSeconds } machine { gpuDisplayName } } }'}
    req = urllib.request.Request(
        "https://api.runpod.io/graphql", data=json.dumps(q).encode(),
        headers={"Authorization": f"Bearer {os.environ['RUNPOD_API_KEY']}",
                 "Content-Type": "application/json", "User-Agent": "runpod-harness/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["data"]["pod"] or {}


def started(pod_id):
    # the watchdog runs inside the container, so a pod stuck before start has no watchdog
    return bool(pod_info(pod_id).get("runtime"))


def gpu_name(pod_id):
    try:
        return (pod_info(pod_id).get("machine") or {}).get("gpuDisplayName") or "GPU"
    except Exception:
        return "GPU"


def launch(idle_min, max_hours):
    pods = api("GET", "/pods") or []
    if pods:
        sys.exit(f"{len(pods)} pod(s) already running - run terminate-all first if you want a new one")
    if input("This starts a paid GPU pod. Type YES to continue: ").strip().lower() != "yes":
        sys.exit("cancelled")
    if login():
        import collector
        print(collector.build(), flush=True)
    else:
        print("CHAT_EMAIL/CHAT_PASSWORD not set: no fixed login and no village feed this time", flush=True)
    for i in range(1, LAUNCH_TRIES + 1):
        try:
            pod = api("POST", "/pods", pod_spec(idle_min, max_hours))
            break
        except urllib.error.HTTPError as e:
            if e.code != 500:
                raise
            print(time.strftime("%H:%M:%S"), f"try {i}/{LAUNCH_TRIES}: no GPU free yet, "
                  "retrying in 60s (costs nothing)", flush=True)
            time.sleep(60)
    else:
        sys.exit("no GPU became free - nothing was created, try again later")
    time.sleep(5)
    print(f"\npod {pod['id']} created on {gpu_name(pod['id'])} at ${pod.get('costPerHr')}/hr\n"
          f"chat link (ready in ~10-15 min): https://{pod['id']}-8080.proxy.runpod.net\n"
          f"Leave this window open: guard is now watching and stops once the pod is gone.\n"
          f"(Closing it is safe too - the pod still deletes itself.)\n", flush=True)
    if login():
        import feeder
        threading.Thread(target=feeder.feed, daemon=True, args=(
            f"https://{pod['id']}-8080.proxy.runpod.net", *login(), MODEL, 32768)).start()
    guard(max_hours)


def guard(max_hours):
    while True:
        try:
            pods = api("GET", "/pods") or []
            if not pods:
                print(time.strftime("%H:%M:%S"), "no pods left - guard done", flush=True)
                return
            for pod in pods:
                created = datetime.strptime(pod["createdAt"][:19], "%Y-%m-%d %H:%M:%S")
                age = (datetime.now(timezone.utc).replace(tzinfo=None) - created).total_seconds() / 60
                if age > max_hours * 60 + 15:
                    reason = f"past time cap ({age:.0f} min)"
                elif age > START_MIN and not started(pod["id"]):
                    reason = f"container not started after {age:.0f} min"
                else:
                    continue
                api("DELETE", f"/pods/{pod['id']}")
                print(time.strftime("%H:%M:%S"), f"deleted {pod['id']}: {reason}", flush=True)
        except Exception as e:
            print(time.strftime("%H:%M:%S"), f"guard check failed: {e}", flush=True)
        time.sleep(30)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["startcmd", "launch", "list", "terminate", "terminate-all", "guard",
                                   "collect"])
    p.add_argument("pod_id", nargs="?")
    p.add_argument("--idle-min", type=float, default=30)
    p.add_argument("--max-hours", type=float, default=3)
    a = p.parse_args()

    if a.cmd == "startcmd":
        print(json.dumps(pod_spec(a.idle_min, a.max_hours), indent=2))
    elif a.cmd == "launch":
        launch(a.idle_min, a.max_hours)
    elif a.cmd == "list":
        for pod in api("GET", "/pods") or []:
            print(pod["id"], pod.get("name"), pod.get("desiredStatus"), gpu_name(pod["id"]),
                  f"${pod.get('costPerHr')}/hr")
    elif a.cmd == "terminate":
        api("DELETE", f"/pods/{a.pod_id or sys.exit('give a POD_ID')}")
        print(f"terminated {a.pod_id}")
    elif a.cmd == "terminate-all":
        for pod in api("GET", "/pods") or []:
            api("DELETE", f"/pods/{pod['id']}")
            print(f"terminated {pod['id']}")
    elif a.cmd == "guard":
        guard(a.max_hours)
    elif a.cmd == "collect":
        import collector
        print(collector.build())


if __name__ == "__main__":
    main()

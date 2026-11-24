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
  python3 pod.py update                                    download the latest harness files (keeps profile.md)

launch/list/terminate need RUNPOD_API_KEY in the environment. startcmd needs nothing.
Uses Runpod's REST v2 API (api.runpod.io/v2); the old REST v1 host no longer resolves.
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
# 48 GB cards; each launch try rents the cheapest one the live catalog shows in stock
GPU_TYPES = ["NVIDIA RTX A6000", "NVIDIA A40"]
MAX_PRICE = 0.59  # $/hr: a card/cloud pair priced above this is never rented
EMBED_MODEL = "nomic-embed-text"  # small model that indexes the #Village library on the GPU
API = "https://api.runpod.io/v2"
START_MIN = 15  # guard deletes a pod whose container hasn't started by then
LAUNCH_TRIES = 20  # one a minute while no GPU is free; failed tries cost nothing
# community hosts are cheaper; Runpod's own (secure) data centers are the fallback
CLOUDS = ["COMMUNITY", "SECURE"]


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
        "https://api.runpod.io/v2/pods/$RUNPOD_POD_ID) & "
        "cd /app/backend && exec bash start.sh"
    )
    return ["bash", "-c", script]


def login():
    email, password = os.environ.get("CHAT_EMAIL"), os.environ.get("CHAT_PASSWORD")
    return (email, password) if email and password else None


def pod_spec(idle_min, max_hours, cloud="COMMUNITY", gpu_id=GPU_TYPES[0]):
    env = {"MODEL": MODEL, "IDLE_MIN": str(idle_min), "MAX_HOURS": str(max_hours),
           "DEFAULT_MODELS": "private-chat",  # Open WebUI preselects it in every chat
           "EMBED_MODEL": EMBED_MODEL, "RAG_EMBEDDING_ENGINE": "ollama", "RAG_EMBEDDING_MODEL": EMBED_MODEL,
           # fewer, batched embedding calls: the 78-file library took 34 min with the defaults
           "RAG_EMBEDDING_BATCH_SIZE": "64", "CHUNK_SIZE": "2000", "CHUNK_OVERLAP": "200"}
    if login():  # Open WebUI creates this admin on first start and turns sign-up off
        env.update(WEBUI_ADMIN_EMAIL=login()[0], WEBUI_ADMIN_PASSWORD=login()[1], WEBUI_ADMIN_NAME="Jason")
    return {  # REST v2 create body; no `mounts`: nothing persists on the pod itself
        "name": "private-chat",
        "image": IMAGE,
        "gpu": {"id": gpu_id, "count": 1},
        "cloud": cloud,
        "disk": 80,
        "ports": ["8080/http"],
        "env": env,
        "cmd": start_cmd(max_hours),  # exec-form CMD, same as the old start command
    }


def api(method, path, body=None):
    key = os.environ.get("RUNPOD_API_KEY") or sys.exit("RUNPOD_API_KEY is not set")
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "runpod-harness/1.0"})  # Cloudflare-fronted: Python's default UA gets 403
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def pods():
    return (api("GET", "/pods") or {}).get("pods") or []  # v2 wraps the list  # rp-migrate: ignore


def started(pod_id):
    # the watchdog runs inside the container, so a pod stuck before start has no watchdog
    pod = api("GET", f"/pods/{pod_id}") or {}
    return pod.get("status") == "RUNNING" or bool(pod.get("runtime"))


STOCK_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}  # anything else (NONE, missing) ranks last


def candidates():
    """(price, cloud, gpu id) for every card/cloud pair offered at or under MAX_PRICE, best stock first,
    then cheapest. Stock only orders the tries: the catalog is an estimate, a failed try costs nothing."""
    found = []
    for cloud in CLOUDS:
        cat = api("GET", f"/catalog/gpus?include=AVAILABILITY&product=POD&count=1&cloud={cloud}") or {}
        for g in cat.get("gpus") or []:
            price = (g.get("price") or {}).get(cloud.lower())
            if g.get("id") in GPU_TYPES and g.get(cloud.lower()) and price is not None and price <= MAX_PRICE:
                found.append((STOCK_RANK.get(g.get("availability"), 3), price, cloud, g["id"]))
    return [(price, cloud, gpu_id) for _, price, cloud, gpu_id in sorted(found)]


def launch(idle_min, max_hours):
    running = pods()
    if running:
        sys.exit(f"{len(running)} pod(s) already running - run terminate-all first if you want a new one")
    if input("This starts a paid GPU pod. Type YES to continue: ").strip().lower() != "yes":
        sys.exit("cancelled")
    if login():
        import collector
        print(collector.build(), flush=True)
    else:
        print("CHAT_EMAIL/CHAT_PASSWORD not set: no fixed login and no village feed this time", flush=True)
    pod = last = None
    for i in range(1, LAUNCH_TRIES + 1):
        try:
            options = candidates()
        except Exception as e:
            options, last = [], f"catalog unavailable: {e}"
        for price, cloud, gpu_id in options:
            try:
                pod = api("POST", "/pods", pod_spec(idle_min, max_hours, cloud, gpu_id))  # rp-migrate: ignore
                break
            except urllib.error.HTTPError as e:
                detail = e.read()[:300].decode("utf-8", "replace")
                if e.code in (401, 403, 422):  # a bad key or body, not scarce GPUs: retrying won't help
                    sys.exit(f"Runpod rejected the request ({e.code}): {detail} - nothing was created")
                last = f"{gpu_id} ({cloud.lower()}): {e.code} {detail[:150]}"
        if pod:
            break
        print(time.strftime("%H:%M:%S"), f"try {i}/{LAUNCH_TRIES}: no GPU free at <= ${MAX_PRICE}/hr yet, "
              "retrying in 60s (costs nothing)" + (f" [{last}]" if last and i == 1 else ""), flush=True)
        time.sleep(60)
    else:
        sys.exit("no GPU became free - nothing was created, try again later")
    print(f"\npod {pod['id']} created on {gpu_id} ({cloud.lower()} cloud) at ${pod.get('cost', price)}/hr\n"
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
            running = pods()
            if not running:
                print(time.strftime("%H:%M:%S"), "no pods left - guard done", flush=True)
                return
            for pod in running:  # createdAt is UTC, "2026-11-24T14:00:00Z" or "2026-11-24 14:00:00 ..."
                created = datetime.strptime(pod["createdAt"][:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
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


UPDATE_URL = "https://raw.githubusercontent.com/givethanksbless/paper-trade-watchlist/main/runpod-harness/"
UPDATE_FILES = ["pod.py", "watchdog.py", "collector.py", "feeder.py"]  # never profile.md: that's yours


def update():
    here = Path(__file__).parent
    fetched = {}
    for name in UPDATE_FILES:  # fetch everything first, so a failed download changes nothing
        req = urllib.request.Request(UPDATE_URL + name, headers={"User-Agent": "runpod-harness/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            fetched[name] = r.read()
    for name, data in fetched.items():
        tmp = here / f".{name}.new"
        tmp.write_bytes(data)
        os.replace(tmp, here / name)
    print("updated:", ", ".join(UPDATE_FILES), "(GitHub can lag a few minutes after a change)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["startcmd", "launch", "list", "terminate", "terminate-all", "guard",
                                   "collect", "update"])
    p.add_argument("pod_id", nargs="?")
    p.add_argument("--idle-min", type=float, default=30)
    p.add_argument("--max-hours", type=float, default=3)
    a = p.parse_args()

    if a.cmd == "startcmd":
        print(json.dumps(pod_spec(a.idle_min, a.max_hours), indent=2))
    elif a.cmd == "launch":
        launch(a.idle_min, a.max_hours)
    elif a.cmd == "list":
        for pod in pods():
            print(pod["id"], pod.get("name"), pod.get("status"), (pod.get("gpu") or {}).get("id", "GPU"),
                  f"${pod.get('cost')}/hr")
    elif a.cmd == "terminate":
        api("DELETE", f"/pods/{a.pod_id or sys.exit('give a POD_ID')}")
        print(f"terminated {a.pod_id}")
    elif a.cmd == "terminate-all":
        for pod in pods():
            api("DELETE", f"/pods/{pod['id']}")
            print(f"terminated {pod['id']}")
    elif a.cmd == "guard":
        guard(a.max_hours)
    elif a.cmd == "collect":
        import collector
        print(collector.build())
    elif a.cmd == "update":
        update()


if __name__ == "__main__":
    main()

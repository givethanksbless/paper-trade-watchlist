#!/usr/bin/env python3
"""Runs INSIDE the pod. Pulls the model, then deletes the pod when idle or past the time cap.

Settings (env vars): MODEL, IDLE_MIN (default 30), MAX_HOURS (default 3).
Uses RUNPOD_POD_ID and RUNPOD_API_KEY, which Runpod injects into every pod.
"Idle" = Ollama has no model loaded. Ollama unloads a model ~5 min after the
last chat message, so the pod is deleted about IDLE_MIN + 5 min after your last message.
If anything in this script crashes, it deletes the pod (money safety first).
If /tmp/profile.md exists, it also creates a "private-chat" model with that profile
as its system prompt, so every chat starts out knowing who you are.
"""
import json
import os
import time
import urllib.request

MODEL = os.environ.get("MODEL", "huihui_ai/deepseek-r1-abliterated:14b")
IDLE_SEC = float(os.environ.get("IDLE_MIN", "30")) * 60
MAX_SEC = float(os.environ.get("MAX_HOURS", "3")) * 3600
POD_ID = os.environ.get("RUNPOD_POD_ID", "")
API_KEY = os.environ.get("RUNPOD_API_KEY", "")
OLLAMA = "http://127.0.0.1:11434"
START = time.time()


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def http(method, url, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "User-Agent": "runpod-harness/1.0",  # default UA gets 403
                 **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def terminate(reason):
    log(f"TERMINATING pod {POD_ID}: {reason}")
    auth = {"Authorization": f"Bearer {API_KEY}"}
    while True:  # once the pod is gone this process dies with it
        try:
            status, _ = http("DELETE", f"https://rest.runpod.io/v1/pods/{POD_ID}", headers=auth)
            log(f"REST delete -> {status}")
        except Exception as e:
            log(f"REST delete failed: {e}")
        try:
            q = {"query": f'mutation {{ podTerminate(input: {{podId: "{POD_ID}"}}) }}'}
            status, out = http("POST", "https://api.runpod.io/graphql", q, headers=auth)
            log(f"GraphQL terminate -> {status} {out[:200]!r}")
        except Exception as e:
            log(f"GraphQL terminate failed: {e}")
        time.sleep(60)


def check_cap():
    age = time.time() - START
    if age >= MAX_SEC:
        terminate(f"time cap reached ({age / 3600:.1f} h)")


def model_loaded():
    _, out = http("GET", f"{OLLAMA}/api/ps", timeout=10)
    return bool(json.loads(out).get("models"))


def create_profile_model():
    # failures only log: a missing profile must never take the pod down
    try:
        with open("/tmp/profile.md") as f:
            profile = f.read().strip()
    except OSError:
        return
    try:  # current Ollama API
        http("POST", f"{OLLAMA}/api/create",
             {"model": "private-chat", "from": MODEL, "system": profile, "stream": False}, timeout=600)
    except Exception as e:
        log(f"profile model (new API) failed: {e}; trying Modelfile form")
        try:  # older Ollama API
            modelfile = f'FROM {MODEL}\nSYSTEM """{profile.replace(chr(34) * 3, chr(39) * 3)}"""\n'
            http("POST", f"{OLLAMA}/api/create",
                 {"name": "private-chat", "modelfile": modelfile, "stream": False}, timeout=600)
        except Exception as e2:
            log(f"profile model failed: {e2} - plain {MODEL} still works")
            return
    log("profile model private-chat ready")


def main():
    log(f"watchdog start: model={MODEL} idle={IDLE_SEC / 60:.0f}min "
        f"cap={MAX_SEC / 3600:.1f}h pod={POD_ID}")
    if not POD_ID or not API_KEY:
        log("WARNING: RUNPOD_POD_ID or RUNPOD_API_KEY missing - cannot self-terminate!")

    while True:  # wait for Ollama to come up
        check_cap()
        try:
            http("GET", f"{OLLAMA}/api/version", timeout=5)
            break
        except Exception:
            time.sleep(5)

    while True:  # pull the model (retry on failure)
        check_cap()
        try:
            log(f"pulling {MODEL} ...")
            http("POST", f"{OLLAMA}/api/pull",
                 {"model": MODEL, "name": MODEL, "stream": False}, timeout=3600)
            log("pull complete - chat is ready")
            break
        except Exception as e:
            log(f"pull failed: {e}; retrying in 30s")
            time.sleep(30)

    create_profile_model()
    last_active = time.time()
    while True:
        check_cap()
        try:
            if model_loaded():
                last_active = time.time()
        except Exception as e:
            log(f"ollama check failed: {e}")
        idle = time.time() - last_active
        if idle >= IDLE_SEC:
            terminate(f"idle for {idle / 60:.0f} min")
        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        terminate(f"watchdog crashed: {e}")

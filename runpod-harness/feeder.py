"""Runs on your computer during launch: pushes memory/ into the pod's chat page.

1. waits for Open WebUI, signs in with CHAT_EMAIL / CHAT_PASSWORD (the fixed admin account)
2. waits for the watchdog's private-chat model, then rebuilds it with profile + briefing
3. uploads memory/archive into a knowledge base called "Village" (#Village in the chat)

Nothing here can delete or stop the pod; a failed step only prints a warning.
"""
import json
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).parent
UA = {"User-Agent": "runpod-harness/1.0"}  # Cloudflare-fronted endpoints reject Python's default


def log(msg):
    print(time.strftime("%H:%M:%S"), "[feed]", msg, flush=True)


def call(url, token=None, body=None, raw=None, ctype="application/json", timeout=60):
    headers = {**UA, "Content-Type": ctype}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = r.read()
    return json.loads(out) if out else None


def wait_for(what, check, minutes=45):
    end = time.time() + minutes * 60
    while time.time() < end:
        try:
            if check():
                return True
        except Exception:
            pass
        time.sleep(20)
    log(f"gave up waiting for {what}")
    return False


def upload(base, token, path):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: text/markdown\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return call(f"{base}/api/v1/files/", token, raw=body,
                ctype=f"multipart/form-data; boundary={boundary}", timeout=600)["id"]


def feed(base, email, password, model, num_ctx):
    memory = HERE / "memory"
    auth = {}

    def signed_in():  # the admin account is created a moment after the page first answers
        auth["token"] = call(f"{base}/api/v1/auths/signin", body={"email": email, "password": password})["token"]
        return True
    if not wait_for("the chat page / sign-in", signed_in):
        return
    token = auth["token"]
    log("signed in; waiting for private-chat (appears once the model has downloaded)")

    def has_private_chat():
        tags = call(f"{base}/ollama/api/tags", token)["models"]
        return any(m["name"].startswith("private-chat") for m in tags)
    if not wait_for("private-chat", has_private_chat):
        return

    profile = (HERE / "profile.md").read_text() if (HERE / "profile.md").exists() else ""
    briefing = (memory / "briefing.md").read_text() if (memory / "briefing.md").exists() else ""
    try:
        call(f"{base}/ollama/api/create", token, timeout=600,
             body={"model": "private-chat", "from": model, "system": f"{profile}\n\n{briefing}".strip(),
                   "parameters": {"num_ctx": num_ctx}, "stream": False})
        log(f"private-chat now has your profile + village briefing ({len(briefing) // 1024} KB)")
    except Exception as e:
        log(f"could not load the briefing into private-chat ({e}) - it still has your profile")

    files = sorted((memory / "archive").rglob("*.md"))
    if not files:
        return
    kb = call(f"{base}/api/v1/knowledge/create", token,
              body={"name": "Village", "description": "Read-only copy of the village: docs, chats, logs"})
    done = 0
    for p in files:
        try:
            fid = upload(base, token, p)
            call(f"{base}/api/v1/knowledge/{kb['id']}/file/add", token, body={"file_id": fid}, timeout=600)
            done += 1
        except Exception as e:
            log(f"skipped {p.name}: {e}")
    log(f"Village library ready: {done}/{len(files)} files - type #Village in a chat to search it")

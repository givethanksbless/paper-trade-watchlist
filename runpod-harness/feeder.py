"""Runs on your computer during launch: pushes memory/ into the pod's chat page.

1. waits for Open WebUI, signs in with CHAT_EMAIL / CHAT_PASSWORD (the fixed admin account)
2. waits for the watchdog's private-chat model, then rebuilds it with profile + briefing
3. uploads memory/archive into a knowledge base called "Village" (#Village in the chat)
Alongside: restores your saved private chats into the new pod, then keeps copying every chat
to private-chats/ on this computer every 2 minutes, so the next pod can pick up where you left off.

Nothing here can delete or stop the pod; a failed step only prints a warning.
"""
import json
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).parent
CHATS_DIR = HERE / "private-chats"  # the only copy of your private conversations; pods are deleted
SAVE_EVERY = 120
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


def write_chat(chat):
    CHATS_DIR.mkdir(exist_ok=True)
    tmp = CHATS_DIR / f".{chat['id']}.tmp"
    tmp.write_text(json.dumps(chat))
    os.replace(tmp, CHATS_DIR / f"{chat['id']}.json")  # never leaves a half-written file


def restore_chats(base, token):
    """Load saved chats into the new pod; each one is re-saved under its new id before the old
    file is removed, so a failure at any point never loses a chat."""
    saved = sorted(CHATS_DIR.glob("*.json")) if CHATS_DIR.is_dir() else []
    done = total = 0
    for p in saved:
        try:  # one chat per file, or a whole "Export Chats" file from the chat page's settings
            data = json.loads(p.read_text())
        except ValueError as e:
            log(f"could not read {p.name}: {e}")
            continue
        olds = data if isinstance(data, list) else [data]
        new_files = []
        for old in olds:
            total += 1
            try:
                new = call(f"{base}/api/v1/chats/import", token, body={
                    "chat": old.get("chat", {}), "meta": old.get("meta") or {}, "pinned": old.get("pinned", False),
                    "folder_id": None, "created_at": old.get("created_at"), "updated_at": old.get("updated_at")})
                write_chat(new)
                new_files.append(f"{new['id']}.json")
            except Exception as e:
                log(f"could not restore a chat from {p.name}: {e}")
        done += len(new_files)
        if len(new_files) == len(olds) and p.name not in new_files:
            p.unlink()  # every chat in it is now saved under its new id
    if total:
        log(f"restored {done}/{total} saved chats - they're in the sidebar")


def save_chats(base, token):
    """Copy every chat to this computer every SAVE_EVERY seconds until the program ends."""
    seen = {}
    while True:
        try:
            chats = call(f"{base}/api/v1/chats/all", token, timeout=120) or []
            changed = [c for c in chats if seen.get(c["id"]) != c.get("updated_at")]
            for c in changed:
                write_chat(c)
                seen[c["id"]] = c.get("updated_at")
            if changed:
                log(f"saved {len(changed)} chat(s) to {CHATS_DIR}")
        except Exception as e:
            log(f"chat save failed ({e}) - will retry")
        time.sleep(SAVE_EVERY)


def feed(base, email, password, model, num_ctx):
    memory = HERE / "memory"
    auth = {}

    def signed_in():  # the admin account is created a moment after the page first answers
        auth["token"] = call(f"{base}/api/v1/auths/signin", body={"email": email, "password": password})["token"]
        return True
    if not wait_for("the chat page / sign-in", signed_in):
        return
    token = auth["token"]
    restore_chats(base, token)
    threading.Thread(target=save_chats, args=(base, token), daemon=True).start()
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

    archive = memory / "archive"
    past = sorted((archive / "private").glob("*.md"))  # your own past chats: a separate library
    village = [p for p in sorted(archive.rglob("*.md")) if p.parent.name != "private"]
    if past:
        library(base, token, "MyChats", "Your past private chats with this model", past)
    if village:
        library(base, token, "Village", "Read-only copy of the village: docs, chats, logs", village)


def library(base, token, name, description, files):
    kb = call(f"{base}/api/v1/knowledge/create", token, body={"name": name, "description": description})
    done, start = 0, time.time()
    log(f"indexing {len(files)} files for #{name} ({sum(p.stat().st_size for p in files) // 1024} KB)")
    for n, p in enumerate(files, 1):
        try:
            fid = upload(base, token, p)
            call(f"{base}/api/v1/knowledge/{kb['id']}/file/add", token, body={"file_id": fid}, timeout=600)
            done += 1
        except Exception as e:
            log(f"skipped {p.name}: {e}")
        if n % 10 == 0:
            log(f"  {n}/{len(files)} files after {(time.time() - start) / 60:.1f} min")
    log(f"{name} library ready: {done}/{len(files)} files in {(time.time() - start) / 60:.1f} min "
        f"- type #{name} in a chat to search it")

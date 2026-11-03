#!/usr/bin/env python3
"""Read-only collector: copies what's going on in the village into ~/runpod-harness/memory.

OBSERVATIONAL ONLY. It reads the village folder and the village's Claude Code chat history
and never writes to either. Everything it copies goes through scrub() first, and files whose
names look like secrets are never read.

  python3 collector.py        build memory/ and print what would be sent (costs nothing)

memory/briefing.md   always loaded into private-chat: trader docs + the last few days
memory/archive/      searchable in the chat as #Village: all docs, full chat history, logs
chat-history/        permanent scrubbed copy of every village chat ever seen; Claude Code deletes
                     chats after 30 days by default, this copy keeps them for the archive
"""
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

VILLAGE = Path(os.environ.get("VILLAGE_DIR", "~/trading")).expanduser()
PROJECTS = Path("~/.claude/projects").expanduser()
CHATS = PROJECTS / str(VILLAGE).replace("/", "-")  # chats started in the village folder: all count
HOME_CHATS = PROJECTS / str(Path.home()).replace("/", "-")  # mixed projects: only village ones
VILLAGE_WORDS = re.compile(r"(?i)village|playa|/trading\b|crypto|bitcoin|\bbtc\b|\beth\b|usdt|\bperps?\b|"
                           r"futures|backtest|forward.?test|hyperliquid|weex|binance|bybit|altcoin|delisting")
MIN_VILLAGE_MENTIONS = 5
OUT = Path(__file__).parent / "memory"
CHAT_CACHE = Path(__file__).parent / "chat-history"  # never wiped, unlike memory/
PRIVATE_CHATS = Path(__file__).parent / "private-chats"  # saved by feeder.py from past pods
CHATS_FILE = Path(__file__).parent / "my-private-chats.md"  # all of them in one file, to upload by hand
BRIEF_DOCS = ["TRADER-PROFILE.md", "ABOUT_PLAYA.md"]
# the live village on the VPS, read over one gentle ssh call per run; set VPS_SSH=user@host to enable
VPS_SSH = os.environ.get("VPS_SSH", "")
VPS_DIR = os.environ.get("VPS_DIR", "/root/trading")
VPS_FILES, VPS_TAIL = 40, 20_000  # newest data files from the last 3 days, last 20 KB of each
VPS_BRIEF_CHARS = 8_000
VPS_SCRIPT = r"""
echo "=== STATUS"; uptime
for s in microstructure-lab weex-bot telegram-relay; do echo "$s: $(systemctl is-active $s 2>/dev/null)"; done
echo "=== SCHEDULED"; crontab -l 2>/dev/null | grep -v "^#" | grep -oE "[^ ]+[.](py|sh|js)" | sort -u
cd "$1" || exit 0
find . -maxdepth 3 -type f \( -name "*.log" -o -name "*.jsonl" -o -name "*.json" \) -mmin -4320 \
  ! -iname "*key*" ! -iname "*secret*" ! -iname "*token*" ! -iname "*.env*" ! -iname "*wallet*" \
  -printf "%T@ %p\n" | sort -rn | head -n "$2" | while read t p; do echo "=== FILE $p"; tail -c "$3" "$p"; echo; done
"""
JASON_PROFILE = Path("~/.claude/jason-profile.md").expanduser()  # Jason's own profile, read-only
PROFILE_CHARS = 10_000
LEFT_OFF_CHARS = 8_000  # the end of your latest private chat, so a new chat picks up from there
NOTES_CHARS = 12_000  # Claude Code's own memory notes about the village, in the briefing
RECENT_DAYS = 3
BRIEF_CHARS = 70_000  # ~18K tokens, leaves ~14K of private-chat's 32K context for the conversation
CHAT_MIN_CHARS = 20_000  # recent village chat always gets at least this much of the briefing
DOC_CHARS = 10_000  # per trader doc in the briefing; the full docs are in the archive
CHAT_ARCHIVE_CHARS = 5_000_000
SESSION_ARCHIVE_CHARS = 1_500_000  # per chat; long chats keep their most recent part
LOG_TAIL_CHARS = 100_000  # per log file
LOG_ARCHIVE_CHARS = 1_000_000
PART_CHARS = 150_000  # archive files are split so each upload finishes inside Runpod's 100 s proxy limit

SECRET_NAME = re.compile(r"(?i)(\.env|secret|credential|passw|token|wallet|keystore|id_rsa|"
                         r"\.pem$|\.key$|\.b64$|\.p12$)")
SECRET_PATTERNS = {
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    "anthropic/openai key": r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}",
    "runpod key": r"\brpa_[A-Za-z0-9]{10,}",
    "github token": r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    "slack token": r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
    "aws key": r"\bAKIA[0-9A-Z]{16}\b",
    "telegram bot token": r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b",
    "bearer token": r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}",
    "64-hex (wallet key/hash)": r"\b(?:0x)?[a-fA-F0-9]{64}\b",
}
# name = value pairs, also inside JSON-escaped text like \"api_key\": \"...\"
KEY_VALUE = re.compile(
    r"(?i)((?:api[_-]?key|secret|token|passw(?:or)?d|passphrase|private[_-]?key|auth[_-]?key)"
    r"[\w-]*[\\\"']*\s*[:=]\s*[\\\"']*)([^\s\"'\\,}]{8,})")
redactions = {}
vps_note = ""
skipped = []  # home-folder chats left out, listed in the summary so the filter can be checked


def scrub(text):
    for name, pat in SECRET_PATTERNS.items():
        text, n = re.subn(pat, "[REDACTED]", text)
        redactions[name] = redactions.get(name, 0) + n
    text, n = KEY_VALUE.subn(lambda m: m.group(1) + "[REDACTED]", text)
    redactions["name=value secret"] = redactions.get("name=value secret", 0) + n
    return text


def write_parts(folder, name, text):
    """Write text as name.md, or name-part01.md, name-part02.md, ... cut at line breaks."""
    stem, parts = name.rsplit(".", 1)[0], []
    while len(text) > PART_CHARS:
        cut = text.rfind("\n", 0, PART_CHARS)
        cut = cut if cut > PART_CHARS // 2 else PART_CHARS
        parts.append(text[:cut])
        text = text[cut:]
    parts.append(text)
    for i, part in enumerate(parts, 1):
        suffix = f"-part{i:02d}" if len(parts) > 1 else ""
        (OUT / "archive" / folder / f"{stem}{suffix}.md").write_text(part)


def read(path, tail=None):
    with open(path, "rb") as f:
        if tail:
            f.seek(max(0, path.stat().st_size - tail))
        return f.read().decode("utf-8", errors="replace")


def village_files(*suffixes):
    return sorted(p for p in VILLAGE.iterdir()
                  if p.is_file() and p.suffix in suffixes and not SECRET_NAME.search(p.name))


def chat_messages(path):
    """Yield (time, speaker, text) for the human-readable parts of a Claude Code transcript."""
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        msg = entry.get("message") or {}
        role = msg.get("role")
        if entry.get("type") not in ("user", "assistant") or role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        parts = [content] if isinstance(content, str) else [
            c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
        text = "\n".join(p for p in parts if p).strip()
        if text:  # tool calls and tool output are skipped: they're bulky and the logs cover them
            yield entry.get("timestamp", ""), "Jason" if role == "user" else "Claude", text


def village_sessions():
    """Every chat in the village folder, plus home-folder chats clearly about the village or
    crypto trading (the village is all crypto trading). Skipped ones are kept for the summary."""
    found = list(CHATS.glob("*.jsonl")) if CHATS.is_dir() else []
    skipped.clear()
    for p in HOME_CHATS.glob("*.jsonl") if HOME_CHATS.is_dir() and HOME_CHATS != CHATS else []:
        text = "\n".join(m[2] for m in chat_messages(p))
        (found if len(VILLAGE_WORDS.findall(text)) >= MIN_VILLAGE_MENTIONS else skipped).append(p)
    return sorted(found, key=lambda p: p.stat().st_mtime)


def chat_turns(chat):
    """(role, text) pairs of an Open WebUI chat. Newer versions keep the thread as a tree in
    chat.history (messages by id, currentId = last message); older ones as a chat.messages list."""
    def text(m):
        c = m.get("content")
        if isinstance(c, list):  # [{"type": "text", "text": ...}, ...]
            c = " ".join(part.get("text", "") for part in c if isinstance(part, dict))
        return c if isinstance(c, str) else ""
    history = chat.get("history") or {}
    by_id, node, thread = history.get("messages") or {}, history.get("currentId"), []
    while node in by_id and len(thread) < 10_000:  # walk from the last message back to the first
        thread.append(by_id[node])
        node = by_id[node].get("parentId")
    msgs = list(reversed(thread)) or [m for m in chat.get("messages") or [] if isinstance(m, dict)]
    return [(m.get("role", "?"), text(m)) for m in msgs if text(m)]


def village_notes():
    """Claude Code's memory notes (projects/*/memory/*.md): all from the village folder's project,
    and from the home folder's project only notes that are about the village or crypto."""
    notes = sorted((CHATS / "memory").glob("*.md")) if (CHATS / "memory").is_dir() else []
    if HOME_CHATS != CHATS and (HOME_CHATS / "memory").is_dir():
        notes += [p for p in sorted((HOME_CHATS / "memory").glob("*.md"))
                  if len(VILLAGE_WORDS.findall(read(p))) >= 3]
    return [p for p in notes if not SECRET_NAME.search(p.name)]


def read_vps():
    """One read-only, low-priority ssh call. Returns (status text, {path: text}) or (reason, {})."""
    if not VPS_SSH:
        return "skipped (VPS_SSH not set)", {}
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", VPS_SSH,
           f"nice -n 19 timeout 90 sh -s -- {VPS_DIR} {VPS_FILES} {VPS_TAIL}"]
    try:
        out = subprocess.run(cmd, input=VPS_SCRIPT, capture_output=True, timeout=120,
                             encoding="utf-8", errors="replace")
    except Exception as e:
        return f"skipped ({e})", {}
    if out.returncode != 0 and "=== STATUS" not in out.stdout:
        return f"skipped (ssh exit {out.returncode}: {out.stderr.strip()[:120]})", {}
    status, files, current = [], {}, None
    for line in out.stdout.splitlines():
        if line.startswith("=== FILE "):
            current = line[9:].lstrip("./")
            files[current] = []
        elif current:
            files[current].append(line)
        else:
            status.append(line)
    return "\n".join(status), {k: "\n".join(v) for k, v in files.items()}


def fmt(ts, speaker, text, limit):
    text = text if len(text) <= limit else text[:limit] + " [...]"
    return f"[{ts[:16].replace('T', ' ')}] {speaker}: {text}"


def build():
    if not VILLAGE.is_dir():
        raise SystemExit(f"village folder not found: {VILLAGE}")
    redactions.clear()
    shutil.rmtree(OUT, ignore_errors=True)
    for sub in ("docs", "chats", "logs", "vps", "private"):
        (OUT / "archive" / sub).mkdir(parents=True)
    cutoff = time.time() - RECENT_DAYS * 86400
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).isoformat()

    # archive: every top-level doc, every chat session (text only), log tails
    for p in village_files(".md"):
        write_parts("docs", p.name, scrub(read(p)))
    notes = village_notes()
    for p in notes:
        write_parts("docs", f"claude-note-{p.parent.parent.name[-8:]}-{p.name}", scrub(read(p)))
    sessions = village_sessions()
    recent_chat = []
    CHAT_CACHE.mkdir(exist_ok=True)
    for p in sessions:  # refresh the permanent copy of every chat Claude Code still has
        msgs = list(chat_messages(p))
        recent_chat += [(m[0], i, m[1], m[2]) for i, m in enumerate(msgs) if m[0] >= cutoff_iso]
        text = scrub("\n\n".join(fmt(*m, 4000) for m in msgs))
        if text:
            cached = CHAT_CACHE / f"chat-{p.stem[:8]}.md"
            cached.write_text(text)
            os.utime(cached, (p.stat().st_mtime, p.stat().st_mtime))
    budget = CHAT_ARCHIVE_CHARS
    for cached in sorted(CHAT_CACHE.glob("chat-*.md"), key=lambda c: -c.stat().st_mtime):
        text = cached.read_text()  # newest first, so the cap drops the oldest
        text = text[-min(SESSION_ARCHIVE_CHARS, max(budget, 0)):] if budget > 0 else ""
        budget -= len(text)
        if text:
            write_parts("chats", cached.name, text)
    past = []
    for p in PRIVATE_CHATS.glob("*.json") if PRIVATE_CHATS.is_dir() else []:
        try:
            data = json.loads(read(p))
        except (ValueError, OSError):
            continue
        for i, c in enumerate(data if isinstance(data, list) else [data]):  # list = "Export Chats" file
            text = "\n\n".join(f"{role}: {t}" for role, t in chat_turns(c.get("chat") or {}))
            if text:
                title = c.get("title") or (c.get("chat") or {}).get("title") or p.stem
                past.append((c.get("updated_at") or 0, c.get("id") or f"{p.stem}-{i}",
                             scrub(f"# Past private chat: {title}\n\n{text}")))
    for _, stem, text in past:
        write_parts("private", f"private-chat-{stem[:8]}.md", text)
    left_off = max(past)[2][-LEFT_OFF_CHARS:] if past else ""
    if past:  # oldest first, newest at the bottom: where you left off
        CHATS_FILE.write_text("\n\n---\n\n".join(t for _, _, t in sorted(past)))
    vps_status, vps_files = read_vps()
    for path, text in vps_files.items():
        write_parts("vps", path.replace("/", "__") + ".md", scrub(text))
    logs, budget = village_files(".log", ".jsonl"), LOG_ARCHIVE_CHARS
    for p in sorted(logs, key=lambda p: -p.stat().st_mtime):
        text = scrub(read(p, LOG_TAIL_CHARS))[:budget]
        budget -= len(text)
        if text:
            write_parts("logs", p.name + ".md", text)

    # briefing: trader docs, what changed, recent log lines, then as much recent chat as fits
    parts = [f"# Village briefing\nRead-only copy collected {time.strftime('%Y-%m-%d %H:%M')} "
             f"covering the last {RECENT_DAYS} days. The 'village chat' below is between Jason and "
             "Claude, his other AI assistant that runs the village - those are NOT conversations with "
             "you. In it, 'Jason' = Jason's instructions and 'Claude' = Claude's replies. Your own past "
             "conversations with Jason are under 'Where you and Jason left off' and in #MyChats. "
             "For anything older or more detailed about the village, search #Village."]
    if left_off:
        parts.append("## Where you and Jason left off (end of your last conversation with him)\n"
                     "'user' is Jason, 'assistant' is you.\n" + left_off)
    if JASON_PROFILE.is_file():
        parts.append("## About Jason (his own profile)\n" + scrub(read(JASON_PROFILE))[:PROFILE_CHARS])
    for name in BRIEF_DOCS:
        if (VILLAGE / name).is_file():
            parts.append(f"## {name}\n" + scrub(read(VILLAGE / name))[:DOC_CHARS])
    if notes:
        text = "\n\n".join(f"### {p.name}\n{read(p).strip()}" for p in notes)
        parts.append("## Claude's memory notes about the village\n" + scrub(text)[:NOTES_CHARS])
    changed = sorted((p for p in VILLAGE.iterdir() if p.is_file() and p.stat().st_mtime > cutoff
                      and not SECRET_NAME.search(p.name)), key=lambda p: -p.stat().st_mtime)
    parts.append("## Village files changed recently\n" + "\n".join(
        f"- {time.strftime('%m-%d %H:%M', time.localtime(p.stat().st_mtime))} {p.name} "
        f"({p.stat().st_size // 1024} KB)" for p in changed[:60]))
    if vps_files or "=== STATUS" in vps_status:
        recent = [f"### {path}\n" + "\n".join(line[:300] for line in text.splitlines()[-8:])
                  for path, text in list(vps_files.items())[:10]]
        parts.append("## Live village on the VPS (read-only snapshot)\n" +
                     scrub(vps_status + "\n\n" + "\n".join(recent))[:VPS_BRIEF_CHARS])
    log_lines = []
    for p in [p for p in logs if p.stat().st_mtime > cutoff][:10]:
        tail = read(p, 6000).splitlines()[-15:]
        log_lines.append(f"### {p.name}\n" + "\n".join(line[:300] for line in tail))
    parts.append("## Recent village log lines\n" + scrub("\n".join(log_lines)))
    head = "\n\n".join(parts)[:BRIEF_CHARS - CHAT_MIN_CHARS]
    chat, room = [], max(BRIEF_CHARS - len(head) - 200, CHAT_MIN_CHARS)
    for ts, _, speaker, text in sorted(recent_chat, reverse=True):  # newest first until full
        line = scrub(fmt(ts, speaker, text, 1500))
        if room - len(line) < 0:
            break
        chat.append(line)
        room -= len(line) + 2
    (OUT / "briefing.md").write_text(
        head + "\n\n## Recent village chat between Jason and Claude (oldest first; not with you)\n"
        + "\n\n".join(reversed(chat)))
    global vps_note
    vps_note = vps_status if vps_status.startswith("skipped") else "connected, no recent data files"
    return summary(sessions)


def preview(p):
    first = next((m[2] for m in chat_messages(p) if m[1] == "Jason"), "")
    first = scrub(" ".join(first.split()))[:70]
    return (f"    {time.strftime('%b %d %H:%M', time.localtime(p.stat().st_mtime))}  "
            f"{p.parent.name[-12:]:>12}  {p.stem[:8]}  \"{first}\"")


def summary(sessions):
    files = [p for p in OUT.rglob("*") if p.is_file()]
    found = {k: v for k, v in redactions.items() if v}  # before the previews below add to it
    lines = [f"collected from {VILLAGE} and {len(sessions)} chat session(s) into {OUT}:"]
    lines += [preview(p) for p in reversed(sessions)]
    if skipped:
        lines.append(f"  skipped {len(skipped)} home-folder chat(s) as not village/crypto:")
        lines += [preview(p) for p in sorted(skipped, key=lambda p: -p.stat().st_mtime)]
    live = {f"chat-{p.stem[:8]}.md" for p in sessions}
    kept = [c for c in CHAT_CACHE.glob("chat-*.md") if c.name not in live]
    lines.append(f"  chat-history: {len(live) + len(kept)} chats kept permanently"
                 + (f" ({len(kept)} already deleted by Claude Code, kept here)" if kept else ""))
    notes = village_notes()
    lines.append(f"  jason-profile.md: {'included' if JASON_PROFILE.is_file() else 'not found'}")
    lines.append(f"  village notes (Claude's memory): {len(notes)} file(s)"
                 + (": " + ", ".join(p.name for p in notes[:8]) if notes else ""))
    lines.append(f"  briefing.md: {(OUT / 'briefing.md').stat().st_size // 1024} KB (always loaded)")
    vps = [p for p in files if p.parent.name == "vps"]
    lines.append(f"  VPS: {len(vps)} file(s), {sum(p.stat().st_size for p in vps) // 1024} KB from {VPS_SSH}"
                 if vps else f"  VPS: {vps_note}")
    if CHATS_FILE.exists():
        lines.append(f"  your past private chats: {CHATS_FILE.read_text().count('# Past private chat:')} saved, "
                     f"all in {CHATS_FILE.name} ({CHATS_FILE.stat().st_size // 1024} KB) - library #MyChats")
    for sub in ("docs", "chats", "logs", "private"):
        fs = [p for p in files if p.parent.name == sub]
        lines.append(f"  archive/{sub}: {len(fs)} files, {sum(p.stat().st_size for p in fs) // 1024} KB")
    lines.append("  secrets redacted: " + (", ".join(f"{v} {k}" for k, v in found.items()) or "none found"))
    return "\n".join(lines)


if __name__ == "__main__":
    print(build())

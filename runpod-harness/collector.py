#!/usr/bin/env python3
"""Read-only collector: copies what's going on in the village into ~/runpod-harness/memory.

OBSERVATIONAL ONLY. It reads the village folder and the village's Claude Code chat history
and never writes to either. Everything it copies goes through scrub() first, and files whose
names look like secrets are never read.

  python3 collector.py        build memory/ and print what would be sent (costs nothing)

memory/briefing.md   always loaded into private-chat: trader docs + the last few days
memory/archive/      searchable in the chat as #Village: all docs, full chat history, logs
"""
import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

VILLAGE = Path(os.environ.get("VILLAGE_DIR", "~/trading")).expanduser()
CHATS = Path("~/.claude/projects").expanduser() / str(VILLAGE).replace("/", "-")
OUT = Path(__file__).parent / "memory"
BRIEF_DOCS = ["TRADER-PROFILE.md", "ABOUT_PLAYA.md"]
RECENT_DAYS = 3
BRIEF_CHARS = 40_000  # ~10K tokens, leaves room in private-chat's 32K context
CHAT_ARCHIVE_CHARS = 3_000_000
LOG_TAIL_CHARS = 100_000  # per log file
LOG_ARCHIVE_CHARS = 1_000_000

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


def scrub(text):
    for name, pat in SECRET_PATTERNS.items():
        text, n = re.subn(pat, "[REDACTED]", text)
        redactions[name] = redactions.get(name, 0) + n
    text, n = KEY_VALUE.subn(lambda m: m.group(1) + "[REDACTED]", text)
    redactions["name=value secret"] = redactions.get("name=value secret", 0) + n
    return text


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


def fmt(ts, speaker, text, limit):
    text = text if len(text) <= limit else text[:limit] + " [...]"
    return f"[{ts[:16].replace('T', ' ')}] {speaker}: {text}"


def build():
    if not VILLAGE.is_dir():
        raise SystemExit(f"village folder not found: {VILLAGE}")
    redactions.clear()
    shutil.rmtree(OUT, ignore_errors=True)
    for sub in ("docs", "chats", "logs"):
        (OUT / "archive" / sub).mkdir(parents=True)
    cutoff = time.time() - RECENT_DAYS * 86400
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).isoformat()

    # archive: every top-level doc, every chat session (text only), log tails
    for p in village_files(".md"):
        (OUT / "archive/docs" / p.name).write_text(scrub(read(p)))
    sessions = sorted(CHATS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if CHATS.is_dir() else []
    recent_chat, budget = [], CHAT_ARCHIVE_CHARS
    for p in reversed(sessions):  # newest first, so the cap drops the oldest
        msgs = list(chat_messages(p))
        recent_chat += [(m[0], i, m[1], m[2]) for i, m in enumerate(msgs) if m[0] >= cutoff_iso]
        text = scrub("\n\n".join(fmt(*m, 4000) for m in msgs))[:budget]
        budget -= len(text)
        if text:
            (OUT / "archive/chats" / f"chat-{p.stem[:8]}.md").write_text(text)
    logs, budget = village_files(".log", ".jsonl"), LOG_ARCHIVE_CHARS
    for p in sorted(logs, key=lambda p: -p.stat().st_mtime):
        text = scrub(read(p, LOG_TAIL_CHARS))[:budget]
        budget -= len(text)
        if text:
            (OUT / "archive/logs" / (p.name + ".txt")).write_text(text)

    # briefing: trader docs, what changed, recent log lines, then as much recent chat as fits
    parts = [f"# Village briefing\nRead-only copy collected {time.strftime('%Y-%m-%d %H:%M')} "
             f"covering the last {RECENT_DAYS} days. Labels: 'Jason' = Jason's instructions, "
             "'Claude' = Claude's replies, 'village files/logs' = raw village output. "
             "For anything older or more detailed, the full archive is searchable as #Village."]
    for name in BRIEF_DOCS:
        if (VILLAGE / name).is_file():
            parts.append(f"## {name}\n" + scrub(read(VILLAGE / name)))
    changed = sorted((p for p in VILLAGE.iterdir() if p.is_file() and p.stat().st_mtime > cutoff
                      and not SECRET_NAME.search(p.name)), key=lambda p: -p.stat().st_mtime)
    parts.append("## Village files changed recently\n" + "\n".join(
        f"- {time.strftime('%m-%d %H:%M', time.localtime(p.stat().st_mtime))} {p.name} "
        f"({p.stat().st_size // 1024} KB)" for p in changed[:60]))
    log_lines = []
    for p in [p for p in logs if p.stat().st_mtime > cutoff][:10]:
        tail = read(p, 6000).splitlines()[-15:]
        log_lines.append(f"### {p.name}\n" + "\n".join(line[:300] for line in tail))
    parts.append("## Recent village log lines\n" + scrub("\n".join(log_lines)))
    head = "\n\n".join(parts)
    chat, room = [], BRIEF_CHARS - len(head) - 200
    for ts, _, speaker, text in sorted(recent_chat, reverse=True):  # newest first until full
        line = scrub(fmt(ts, speaker, text, 1500))
        if room - len(line) < 0:
            break
        chat.append(line)
        room -= len(line) + 2
    (OUT / "briefing.md").write_text(
        head + "\n\n## Recent village chat (oldest first)\n" + "\n\n".join(reversed(chat)))
    return summary(len(sessions))


def summary(n_sessions):
    files = [p for p in OUT.rglob("*") if p.is_file()]
    lines = [f"collected from {VILLAGE} and {n_sessions} chat session(s) into {OUT}:",
             f"  briefing.md: {(OUT / 'briefing.md').stat().st_size // 1024} KB (always loaded)"]
    for sub in ("docs", "chats", "logs"):
        fs = [p for p in files if p.parent.name == sub]
        lines.append(f"  archive/{sub}: {len(fs)} files, {sum(p.stat().st_size for p in fs) // 1024} KB")
    found = {k: v for k, v in redactions.items() if v}
    lines.append("  secrets redacted: " + (", ".join(f"{v} {k}" for k, v in found.items()) or "none found"))
    return "\n".join(lines)


if __name__ == "__main__":
    print(build())

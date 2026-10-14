#!/usr/bin/env python3
"""continuity watchdog (kirk cron, every 10 min).

Checks the things that silently broke on 2026-10-14 and the things that would hide
the next break, self-heals what it can, and only alerts (once, deduped) when a heal
fails. A "recovered" message is sent only for checks that had alerted.

  mcp_config   ~/.claude.json continuity-v2 interpreter can import sqlite_vec+numpy
               (heal: repoint to repo .venv; a brew python upgrade broke bare python3)
  mcp_search   MCP stdio handshake + hyphenated search returns real matches
  api          continuity-api /health 200 and hyphenated /search returns results
               (heal: launchctl kickstart -k com.philmcneely.continuity-api)
  index_fresh  newest indexed turn < 3h old (heal: run pipeline sync-only in background)
  edges        TEMPORAL edges table populated, so thread_recall works (heal: wire_edges.py)
  pipeline     last pipeline run < 2h old and finished without errors

Status JSON: ~/Library/Logs/continuity-watchdog.json   Log: ~/Library/Logs/continuity-watchdog.log
Usage: continuity_watchdog.py [--no-heal] [--no-alert]
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parent
VENV_PY = REPO / ".venv" / "bin" / "python"
DB = REPO / "data" / "continuity.db"
CLAUDE_JSON = HOME / ".claude.json"
API = "http://localhost:8767"
PIPE_LOG = HOME / "Library" / "Logs" / "continuity-pipeline.log"
LOG = HOME / "Library" / "Logs" / "continuity-watchdog.log"
STATUS = HOME / "Library" / "Logs" / "continuity-watchdog.json"
STATE = HOME / ".claude" / "hooks" / "state" / "continuity-watchdog-state.json"
PROBE = "continuity-v2"  # hyphenated on purpose: the 2026-10-14 FTS5 bug class
INDEX_MAX_AGE_H = 3
PIPE_MAX_AGE_H = 2

HEAL = "--no-heal" not in sys.argv
ALERT = "--no-alert" not in sys.argv


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(time.strftime("%F %T ") + msg + "\n")


def run(cmd, timeout=60, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


# ------------------------------------------------------------------ checks
def check_mcp_config():
    cfg = json.loads(CLAUDE_JSON.read_text())["mcpServers"]["continuity-v2"]
    py = cfg.get("command", "")
    r = run([py, "-c", "import sqlite_vec, numpy"], timeout=30) if Path(py).exists() or "/" not in py else None
    if r is not None and r.returncode == 0:
        return True, f"interpreter ok: {py}"
    return False, f"interpreter cannot import deps: {py}"


def heal_mcp_config():
    data = json.loads(CLAUDE_JSON.read_text())
    bak = CLAUDE_JSON.with_name(f".claude.json.bak-watchdog-{time.strftime('%Y%m%d-%H%M%S')}")
    bak.write_text(json.dumps(data, indent=2))
    data["mcpServers"]["continuity-v2"]["command"] = str(VENV_PY)
    CLAUDE_JSON.write_text(json.dumps(data, indent=2))
    return f"repointed MCP to {VENV_PY} (backup {bak.name})"


def check_mcp_search():
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "watchdog", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "search_sessions", "arguments": {"query": PROBE, "limit": 1}}},
    ]
    cfg = json.loads(CLAUDE_JSON.read_text())["mcpServers"]["continuity-v2"]
    p = subprocess.Popen([cfg["command"], *cfg["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env={**os.environ, **cfg.get("env", {})})
    try:
        out, err = p.communicate("\n".join(json.dumps(m) for m in msgs) + "\n", timeout=60)
    except subprocess.TimeoutExpired:
        p.kill()
        return False, "MCP search timed out"
    for line in out.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("id") == 2:
            text = (m.get("result") or {}).get("content", [{}])[0].get("text", "")
            if "error" in text.lower() or "match(es)" not in text:
                return False, f"MCP search bad result: {text[:120]!r}"
            return True, "MCP hyphenated search returns matches"
    return False, f"MCP gave no search reply; stderr: {err.strip()[-160:]!r}"


def _http(method, path, body=None, timeout=15):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"{}")


def check_api():
    try:
        st, _ = _http("GET", "/health")
        st2, res = _http("POST", "/search", {"query": PROBE, "limit": 1})
    except Exception as e:  # noqa: BLE001 - any failure is a failed check
        return False, f"API error: {e!r}"[:200]
    if st != 200 or st2 != 200 or not res.get("results"):
        return False, f"API health={st} search={st2} results={len(res.get('results') or [])}"
    return True, "API health + hyphenated search ok"


def heal_api():
    r = run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.philmcneely.continuity-api"], timeout=30)
    time.sleep(6)
    return f"kickstart rc={r.returncode}"


def _db():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    return con


def check_index_fresh():
    ts = _db().execute("SELECT max(ts) FROM turns").fetchone()[0]
    newest = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    age_h = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
    return age_h < INDEX_MAX_AGE_H, f"newest turn {age_h:.1f}h old"


def heal_index_fresh():
    subprocess.Popen(["bash", str(REPO / "continuity-pipeline.sh"), "sync-only"],
                     stdout=open(PIPE_LOG, "a"), stderr=subprocess.STDOUT, start_new_session=True)
    return "started pipeline sync-only in background"


def check_edges():
    con = _db()
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='edges'").fetchone():
        return False, "edges table missing (thread_recall broken)"
    n = con.execute("SELECT count(*) FROM edges").fetchone()[0]
    return n > 0, f"{n} edges"


def heal_edges():
    r = run([str(VENV_PY), str(REPO / "wire_edges.py")], timeout=300)
    return f"wire_edges rc={r.returncode}"


def check_pipeline():
    if not PIPE_LOG.exists():
        return False, "pipeline log missing"
    with open(PIPE_LOG, "rb") as f:
        f.seek(max(0, PIPE_LOG.stat().st_size - 400_000))
        tail = f.read().decode(errors="replace")
    ends = re.findall(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] === Pipeline complete( with (\d+) error)?", tail)
    if not ends:
        return False, "no completed pipeline run in recent log"
    when, _, errs = ends[-1]
    age_h = (datetime.now() - datetime.strptime(when, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
    if age_h > PIPE_MAX_AGE_H:
        return False, f"last pipeline run {age_h:.1f}h ago"
    if errs:
        return False, f"last pipeline run had {errs} error(s)"
    return True, f"last pipeline run {age_h:.1f}h ago, clean"


CHECKS = [
    ("mcp_config", check_mcp_config, heal_mcp_config),
    ("mcp_search", check_mcp_search, None),
    ("api", check_api, heal_api),
    ("index_fresh", check_index_fresh, heal_index_fresh),
    ("edges", check_edges, heal_edges),
    ("pipeline", check_pipeline, None),
]


# ------------------------------------------------------------------ alerting
def send(msg):
    if not ALERT:
        return
    sys.path.insert(0, str(HOME / "git" / "claude-agent" / "tools"))
    try:
        from telegram_log import send_and_log  # fleet's logged Telegram sender
        env = {}
        for line in (HOME / "git" / "claude-agent" / ".env").read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        chat = env.get("FLEETOPS_CHAT_ID") or env.get("TELEGRAM_CHAT_ID", "")
        send_and_log(msg, source="continuity-watchdog", chat_id=chat)
    except Exception as e:  # noqa: BLE001 - never let alerting crash the watchdog
        log(f"telegram send failed ({e!r}); macOS notification instead")
        run(["osascript", "-e", f'display notification "{msg[:180]}" with title "Continuity watchdog"'], timeout=10)


def main():
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    results = {}
    for name, check, heal in CHECKS:
        try:
            ok, detail = check()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"check crashed: {e!r}"[:200]
        healed = None
        if not ok and HEAL and heal:
            try:
                healed = heal()
                ok, detail = check()
            except Exception as e:  # noqa: BLE001
                healed = f"heal crashed: {e!r}"[:200]
            log(f"{name}: heal -> {healed}; now ok={ok} ({detail})")
        results[name] = {"ok": ok, "detail": detail, "healed": healed}
        was_alerted = state.get(name, {}).get("alerted", False)
        if not ok and not was_alerted:
            send(f"⚠️ continuity watchdog: {name} FAILED and self-heal did not fix it — {detail}")
            state[name] = {"alerted": True, "since": time.strftime("%F %T")}
        elif ok and was_alerted:
            send(f"✅ continuity watchdog: {name} recovered — {detail}")
            state[name] = {"alerted": False}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))
    STATUS.write_text(json.dumps({"ts": time.strftime("%F %T"), "checks": results}, indent=2))
    bad = [n for n, r in results.items() if not r["ok"]]
    log("OK all checks" if not bad else f"FAILING: {bad}")
    print(json.dumps(results, indent=2))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

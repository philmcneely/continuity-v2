"""Extract key facts from indexed Continuity sessions and store in fleet memory.

Reads recent sessions from the Continuity SQLite DB, sends turns to an LLM
for fact extraction, and POSTs extracted facts to the fleet memory REST API.

Usage:
    python extract-to-fleet-memory.py                    # extract from last 24h
    python extract-to-fleet-memory.py --hours 48         # extract from last 48h
    python extract-to-fleet-memory.py --session <id>     # extract from specific session
"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "continuity.db"
STATE_FILE = Path(__file__).parent / "data" / "extraction-state.json"
FLEET_MEMORY_URL = os.environ.get("FLEET_MEMORY_URL", "http://192.168.0.240:8766")
LLM_URL = os.environ.get("LLM_URL", "http://localhost:3020/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "auto")

EXTRACTION_PROMPT = """You are a knowledge extractor. Given a conversation transcript between a user (Phil) and an AI assistant, extract ONLY facts worth remembering for future sessions.

Extract these types of facts:
1. DECISIONS — architectural choices, tool selections, approach decisions
2. CONFIGS — service ports, file paths, credentials locations, machine configs discovered
3. OUTCOMES — what worked, what failed, root causes found
4. CORRECTIONS — when Phil corrected the AI's approach or understanding
5. DISCOVERIES — unexpected findings about the codebase, infrastructure, or tools

DO NOT extract:
- Debugging dead ends that led nowhere
- Conversation noise or pleasantries
- Things already obvious from code (function names, file contents)
- Ephemeral task state (what's currently running, in-progress work)
- Anything that would be stale in a week

Return a JSON array of objects, each with:
- "fact": the key fact in one clear sentence
- "type": one of [decision, config, outcome, correction, discovery]
- "tags": array of relevant tags (machine names, tool names, project names)

If there are no facts worth extracting, return an empty array: []

TRANSCRIPT:
{transcript}"""


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_extracted": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def get_recent_sessions(conn, hours=24, session_id=None):
    if session_id:
        return conn.execute(
            "SELECT id, ai_title, node, started_at, ended_at, turn_count FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchall()

    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    return conn.execute(
        "SELECT id, ai_title, node, started_at, ended_at, turn_count FROM sessions "
        "WHERE ended_at > ? AND turn_count >= 4 ORDER BY ended_at DESC",
        (cutoff,),
    ).fetchall()


def get_session_text(conn, session_id, max_chars=12000):
    turns = conn.execute(
        "SELECT role, text FROM turns WHERE session_id = ? ORDER BY turn_idx",
        (session_id,),
    ).fetchall()

    lines = []
    total = 0
    for role, text in turns:
        if not text or len(text.strip()) < 10:
            continue
        chunk = text[:2000]
        if total + len(chunk) > max_chars:
            break
        lines.append(f"[{role}]: {chunk}")
        total += len(chunk)

    return "\n\n".join(lines)


def extract_facts_via_llm(transcript):
    prompt = EXTRACTION_PROMPT.format(transcript=transcript)
    payload = json.dumps(
        {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 2000,
        }
    )

    result = subprocess.run(
        [
            "curl",
            "-s",
            "-X",
            "POST",
            LLM_URL,
            "-H",
            "Content-Type: application/json",
            "-d",
            payload,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        print(f"  LLM request failed: {result.stderr[:200]}", file=sys.stderr)
        return []

    try:
        resp = json.loads(result.stdout)
        content = resp["choices"][0]["message"]["content"]
        start = content.find("[")
        end = content.rfind("]") + 1
        if start >= 0 and end > start:
            return json.loads(content[start:end])
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  Failed to parse LLM response: {e}", file=sys.stderr)

    return []


def store_in_fleet_memory(fact, session_id, node):
    fact_type = fact.get("type", "unknown")
    fact_text = fact.get("fact", "")
    if not fact_text:
        print(f"  SKIP: fact missing 'fact' field: {fact!r}", file=sys.stderr)
        return False
    memory_content = f"[{fact_type}] {fact_text}"
    tags = fact.get("tags", [])
    tags.extend(["continuity-extract", f"node:{node}"])

    payload = json.dumps(
        {
            "content": memory_content,
            "metadata": {
                "type": fact_type,
                "source": "continuity-extract",
                "session_id": session_id,
                "tags": tags,
                "extracted_at": datetime.now().isoformat(),
            },
        }
    )

    result = subprocess.run(
        [
            "curl",
            "-s",
            "-X",
            "POST",
            f"{FLEET_MEMORY_URL}/store",
            "-H",
            "Content-Type: application/json",
            "-d",
            payload,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    return result.returncode == 0


def main():
    import argparse

    ap = argparse.ArgumentParser(
        description="Extract facts from Continuity sessions into fleet memory"
    )
    ap.add_argument(
        "--hours", type=int, default=24, help="Look back N hours (default: 24)"
    )
    ap.add_argument("--session", type=str, help="Extract from a specific session ID")
    ap.add_argument("--dry-run", action="store_true", help="Extract but don't store")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"Continuity DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")

    state = load_state()
    sessions = get_recent_sessions(conn, hours=args.hours, session_id=args.session)

    print(f"Found {len(sessions)} sessions to process")

    total_facts = 0
    total_stored = 0

    for sid, title, node, started, ended, turns in sessions:
        if sid in state["last_extracted"] and not args.session:
            print(f"  SKIP {sid[:12]} (already extracted)")
            continue

        print(
            f"  Processing: {sid[:12]} [{node}] {title or 'untitled'} ({turns} turns)"
        )

        transcript = get_session_text(conn, sid)
        if len(transcript) < 200:
            print(f"    Too short, skipping")
            state["last_extracted"][sid] = datetime.now().isoformat()
            continue

        facts = extract_facts_via_llm(transcript)
        total_facts += len(facts)
        print(f"    Extracted {len(facts)} facts")

        for fact in facts:
            if "type" not in fact and "fact" not in fact:
                print(
                    f"    QUARANTINE: malformed fact (missing both 'type' and 'fact' keys): {fact!r}",
                    file=sys.stderr,
                )
                continue
            if args.dry_run:
                print(f"    [{fact.get('type', 'unknown')}] {fact.get('fact', '')}")
                print(f"      tags: {fact.get('tags', [])}")
            else:
                if store_in_fleet_memory(fact, sid, node or "unknown"):
                    total_stored += 1
                    print(f"    STORED: [{fact.get('type', 'unknown')}] {fact.get('fact', '')[:80]}")
                else:
                    print(f"    FAILED: [{fact.get('type', 'unknown')}] {fact.get('fact', '')[:80]}")

        state["last_extracted"][sid] = datetime.now().isoformat()

    if not args.dry_run:
        save_state(state)

    print(f"\nDone. Facts extracted: {total_facts}, stored: {total_stored}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

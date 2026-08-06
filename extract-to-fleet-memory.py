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
# NOTE: fleet memory moved to HAL (see continuity-pipeline.sh); .240 is a
# different, unrelated mcp-memory instance. Keep this default in sync with
# the pipeline's export so a standalone/manual run doesn't silently write
# to the wrong store.
FLEET_MEMORY_URL = os.environ.get("FLEET_MEMORY_URL", "http://192.168.0.225:8766")
LLM_URL = os.environ.get("LLM_URL", "http://localhost:3020/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "auto")

# Safety valves so a long backlog (e.g. first run after deploying the
# incremental fix, or a fleet node coming back after an outage) can't
# stampede the LLM or flood fleet memory in one run.
MAX_CHARS_PER_BATCH = 12000
MAX_TURNS_PER_BATCH = 150
MAX_SESSIONS_PER_RUN = 40

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
        state = json.loads(STATE_FILE.read_text())
    else:
        state = {"last_extracted": {}}
    # "last_extracted" (legacy) marks a session fully SKIPPED forever after
    # one pass — that's the bug: a session alive for weeks only ever got its
    # first ~12000 chars looked at, then was never revisited. The new key
    # tracks the highest turn_idx already extracted per session, so growth
    # keeps getting picked up incrementally instead of the session going dark.
    state.setdefault("last_extracted_turn_idx", {})
    return state


def migrate_legacy_state(conn, state):
    """One-time backfill for the old session-level skip list.

    For sessions the legacy code already touched at least once, seed their
    turn_idx marker at their CURRENT max turn_idx (i.e. "caught up as of
    now"). This deliberately does NOT retroactively re-extract everything
    those sessions ever said — that would stampede the LLM and flood fleet
    memory with facts from months of already-reviewed history. It just stops
    them from being skipped forever: any turn added AFTER this migration
    point gets picked up on the next run that sees it.
    """
    legacy = state.get("last_extracted", {})
    marker = state["last_extracted_turn_idx"]
    migrated = 0
    for sid in legacy:
        if sid in marker:
            continue
        row = conn.execute(
            "SELECT turn_count FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        if not row:
            continue
        marker[sid] = row[0] - 1  # turn_idx is 0-based
        migrated += 1
    if migrated:
        print(f"Migrated {migrated} legacy session markers to turn-incremental tracking")
    return state


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


def get_session_text(
    conn,
    session_id,
    since_turn_idx=-1,
    max_chars=MAX_CHARS_PER_BATCH,
    max_turns=MAX_TURNS_PER_BATCH,
):
    """Return (transcript_text, last_turn_idx_included) for turns AFTER since_turn_idx.

    Turn indices are stable across reindexes (transcripts are append-only, and
    index.py re-numbers from 0 in file order each time), so "last extracted
    turn_idx" is a safe incremental watermark. Bounded by max_chars/max_turns
    per run; last_turn_idx_included reflects exactly what was sent to the LLM
    so the caller can advance the marker without skipping anything, even if a
    session has a large backlog that takes several runs to fully catch up on.
    """
    turns = conn.execute(
        "SELECT turn_idx, role, text FROM turns WHERE session_id = ? AND turn_idx > ? "
        "ORDER BY turn_idx",
        (session_id, since_turn_idx),
    ).fetchall()

    lines = []
    total = 0
    last_idx = since_turn_idx
    for turn_idx, role, text in turns:
        if not text or len(text.strip()) < 10:
            last_idx = turn_idx
            continue
        chunk = text[:2000]
        if total + len(chunk) > max_chars or len(lines) >= max_turns:
            break
        lines.append(f"[{role}]: {chunk}")
        total += len(chunk)
        last_idx = turn_idx

    return "\n\n".join(lines), last_idx


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

    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "-X",
                "POST",
                LLM_URL,
                "-H",
                "Content-Type: application/json",
                # Identify this job to the proxy so Switchboard can attribute its
                # spend. It runs every 2h over every new session, so it is a real
                # recurring cost — without this header it lands under "curl" and
                # becomes indistinguishable from every other ad-hoc call.
                "-H",
                "X-Caller: fact-extraction",
                "-d",
                payload,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        # A single slow/overloaded model must not take down the whole run —
        # one session timing out used to crash main() before it reached any
        # of the other sessions in the batch, i.e. one bad LLM call could
        # zero out an entire run's stored-fact count.
        print("  LLM request timed out after 120s, skipping this batch", file=sys.stderr)
        return []

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
    """Store one fact. Returns 'stored', 'duplicate', or 'failed'.

    Fleet memory rejects exact-content duplicates server-side (content-hash
    match) — we surface that as 'duplicate' rather than counting it as a
    fresh store, so run-over-run counts stay honest. This is a safety net,
    not the primary defense: turn-incremental extraction (see
    get_session_text) means the same turns are never re-sent to the LLM,
    which is what actually keeps re-extraction from happening at all.
    """
    fact_type = fact.get("type", "unknown")
    fact_text = fact.get("fact", "")
    if not fact_text:
        print(f"  SKIP: fact missing 'fact' field: {fact!r}", file=sys.stderr)
        return "failed"
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

    if result.returncode != 0:
        return "failed"

    try:
        resp = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Older mock/backend that just returns an opaque id — treat any
        # 2xx-shaped curl success without a parseable body as stored, to
        # avoid breaking against a backend that doesn't send "success".
        return "stored"

    if resp.get("success") is False:
        if "duplicate" in resp.get("message", "").lower():
            return "duplicate"
        return "failed"
    return "stored"


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
    ap.add_argument(
        "--max-sessions",
        type=int,
        default=MAX_SESSIONS_PER_RUN,
        help=f"Cap sessions with new turns processed per run (default: {MAX_SESSIONS_PER_RUN})",
    )
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"Continuity DB not found: {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")

    state = load_state()
    state = migrate_legacy_state(conn, state)
    markers = state["last_extracted_turn_idx"]
    sessions = get_recent_sessions(conn, hours=args.hours, session_id=args.session)

    print(f"Found {len(sessions)} sessions in window")

    total_facts = 0
    total_stored = 0
    total_duplicate = 0
    total_failed = 0
    processed = 0

    for sid, title, node, started, ended, turns in sessions:
        max_idx = turns - 1  # turn_idx is 0-based, contiguous per session
        last_done = markers.get(sid, -1)

        if not args.session and last_done >= max_idx:
            print(f"  SKIP {sid[:12]} (no new turns past idx {last_done})")
            continue

        if processed >= args.max_sessions:
            print(f"  SKIP {sid[:12]} (hit --max-sessions {args.max_sessions} for this run, will pick up next run)")
            continue
        processed += 1

        since = -1 if args.session else last_done
        new_turn_count = max_idx - since
        print(
            f"  Processing: {sid[:12]} [{node}] {title or 'untitled'} "
            f"({new_turn_count} new turns since idx {since})"
        )

        transcript, last_idx = get_session_text(conn, sid, since_turn_idx=since)
        if len(transcript) < 200:
            print(f"    Too short, skipping")
            markers[sid] = last_idx
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
                status = store_in_fleet_memory(fact, sid, node or "unknown")
                label = fact.get('fact', '')[:80]
                if status == "stored":
                    total_stored += 1
                    print(f"    STORED: [{fact.get('type', 'unknown')}] {label}")
                elif status == "duplicate":
                    total_duplicate += 1
                    print(f"    DUPLICATE (already in fleet memory): [{fact.get('type', 'unknown')}] {label}")
                else:
                    total_failed += 1
                    print(f"    FAILED: [{fact.get('type', 'unknown')}] {label}")

        if not args.dry_run:
            markers[sid] = last_idx

    if not args.dry_run:
        save_state(state)

    print(
        f"\nDone. Facts extracted: {total_facts}, stored: {total_stored} "
        f"(duplicates: {total_duplicate}, failed: {total_failed})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

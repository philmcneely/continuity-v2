<p align="center">
  <img src="assets/banner.png" alt="continuity v2" width="100%">
</p>

# continuity v2

Long-term memory layer for Claude Code, built on the JSONL session record.

## What this is

`continuity` (v1) protected against compaction *within* a single session — PreCompact hook saves a checkpoint, SessionStart hook injects it back, SSE proxy rings bells at 70/85/95% token pressure.

`continuity v2` solves the other half: **across-session recall.**

Every Claude Code session is already written to disk as a JSONL — every turn, every tool call, every response. Plain text. Append-only. Free.

That file is an episodic memory store hiding in plain sight. v2 is the index, search, and retrieval layer on top of it.

## Architecture

```
~/.claude/projects/<project-id>/<session-id>.jsonl   (raw episodic record, already there)
                       ↓
                   v2 indexer
                       ↓
              SQLite + FTS5 + (later) embeddings
                       ↓
                  MCP tool surface
                       ↓
        Claude calls search_sessions(query) mid-conversation
```

## Build stages

### Stage 1 — Index (MVP)

Walk `~/.claude/projects/`, extract every turn into SQLite with FTS5:

```
sessions(id, project, started_at, ended_at, turn_count)
turns(session_id, turn_idx, ts, role, text)
turns_fts (FTS5 mirror of turns.text)
```

Literal full-text search across every conversation ever had with Claude Code. Filterable by project, reverse-chronological.

### Stage 2 — MCP tool

Wire it into the existing `claude-memory` MCP (port 8200) or stand alone.

- `search_sessions(query, limit=10)` — FTS5 search, returns matching turns + session ID + surrounding context
- `recall_session(session_id, range="full")` — return full or sliced session
- `recent_sessions(n=10)` — list recent sessions with first/last user message

### Stage 3 — Semantic recall

Sentence embeddings + SIMILAR_TO edges. Hybrid search: literal + semantic.

For when the words don't match but the concept does ("when did we talk about the thread-home problem" should match conversations about "Hal", "50 First Dates", "Alice in Wonderland").

`find_similar(query, limit=10)` — find semantically similar turns via ANN search over
`all-MiniLM-L6-v2` embeddings, re-ranked by a hybrid score:

```
score = 0.7 * semantic_similarity
      + 0.2 * recency_decay       # linear, full decay at 365 days
      + 0.1 * complexity_bonus    # session turn count, normalized to 50 turns
```

Build: `python embed.py` (idempotent — skips turns already embedded), then
`python wire_similar.py` to wire `SIMILAR_TO` edges between turn pairs with cosine ≥ 0.85.

### Stage 4 — Auto-tagging (later)

- Project mentions (saranna, dream-module, gold-402, etc.)
- Decision markers (`DECIDED`, `SAVE`, session-close triggers)
- File paths touched

## Why this matters

Compaction stops being context loss and becomes a cache miss. The 3-hour conversation isn't gone — it's a `search_sessions()` call away.

The "I told you this two weeks ago" problem disappears. The episodic record was always there. Nothing was ever lost. Nobody had wired it into recall yet.

## Status

**Stages 1, 2, 3, and 3A complete.** Sessions grow with every conversation; DB starts around 50 MB.

Two sources in one DB:
- **Claude Code sessions** -- JSONL files under `~/.claude/projects/`
- **claude.ai chat conversations** -- Anthropic data export (`conversations.json`)

```
python index.py                              # index Claude Code sessions (incremental)
python chat_index.py <path/conversations.json>  # index claude.ai chat export (incremental)
python embed.py                              # build/update sentence embeddings (idempotent)
python wire_edges.py                         # wire TEMPORAL edges between turns
python wire_similar.py                       # wire SIMILAR_TO edges from embeddings (run after embed.py)
python stats.py                              # health + recent sessions
python search.py "<query>"                   # FTS5 search across all sessions
python recall.py <id>                        # full or sliced session by id

# FTS5 quoting note: hyphenated/numeric tokens MUST be double-quoted
#   python search.py '"memory-v4" wave propagation' --project C--dev
#   python search.py '"gold-402" distribution'
```

DB lives at `data/continuity.db` (gitignored).

To get your claude.ai chat export: claude.ai -> Settings -> Export data.

### Stage 2 -- MCP server

`mcp_server.py` exposes the index as a stdio MCP server. Add to `~/.claude.json`
(the primary MCP config for Claude Code — **not** `~/.claude/settings.json`):

```json
"mcpServers": {
  "continuity-v2": {
    "type": "stdio",
    "command": "python",
    "args": ["/path/to/continuity-v2/mcp_server.py"],
    "env": { "PYTHONIOENCODING": "utf-8" }
  }
}
```

Tools exposed:

- `search_sessions(query, limit=10, project=None, source=None, node=None)` -- FTS5 search with snippets
- `find_similar(query, limit=10)` -- semantic search, hybrid-scored (0.7 sem + 0.2 recency + 0.1 complexity)
- `thread_recall(query, ...)` -- BFS over TEMPORAL edges; returns narrative thread not just rows
- `recall_session(session_id, idx_from=None, idx_to=None)` -- full or sliced replay
- `recent_sessions(n=10, project=None, source=None, node=None)` -- list recent sessions
- `index_stats()` -- DB health, session/turn counts, embedding coverage, edge counts, per-node breakdown
- `reindex()` -- re-index new sessions without restarting the server (respects `CONTINUITY_ROOT`/`CONTINUITY_NODE` env vars)

The `source` param accepts `"code"` (Claude Code only) or `"chat"` (claude.ai only). Omit for both.

Restart Claude Code to load the server.


### Fleet / Multi-Node Support

If you run Claude Code on multiple machines, you can centralize all sessions into a single index with per-node tagging.

**How it works:**

```
Machine A (kirk)        Machine B (mac-studio)       Machine C (hal9000)
~/.claude/projects/     ~/.claude/projects/          ~/.claude/projects/
        ↓  rsync                ↓  rsync                     ↓  rsync
        └───────────────────────┴─────────────────────────────┘
                                ↓
                Central store:  /Volumes/Data/continuity-sessions/
                    kirk/           ← mirrored from A
                    mac-studio/     ← mirrored from B
                    hal9000/        ← mirrored from C
                                ↓
                python index.py --root <central-store>/kirk --node kirk
                python index.py --root <central-store>/mac-studio --node mac-studio
                                ↓
                        Single continuity.db
                        (all nodes, all sessions)
```

**Environment variables:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `CONTINUITY_ROOT` | `~/.claude/projects` | Root directory to scan for JSONL files |
| `CONTINUITY_NODE` | `local` | Node name tag applied to indexed sessions |

**CLI usage:**

```bash
# Index from a specific root with a node tag
python index.py --root /Volumes/Data/continuity-sessions/kirk --node kirk

# Search filtered by node
python search.py "auth middleware" --node kirk

# MCP tools accept node= parameter too
# search_sessions(query="auth", node="kirk")
# recent_sessions(n=10, node="mac-studio")
```

**Sync script:**

`sync-fleet.sh` rsyncs JSONL files from configured nodes to a central store, then indexes each:

```bash
./sync-fleet.sh              # sync all configured nodes
./sync-fleet.sh kirk         # sync one node
```

Edit the `NODES` associative array in the script to add your machines. Set `CONTINUITY_CENTRAL_STORE` env var or edit the default path.

**Coexistence with single-node setup:** The fleet setup is additive. A single-machine install with no env vars works exactly as before (node defaults to `"local"`). The `node` column is auto-added to existing DBs on first run via schema migration.

**`index_stats()` output** now includes a per-node breakdown:

```
Sessions:      659 (code: 655, chat: 4)
Nodes:         kirk=659
```


## Hooks: installation

Copy the four scripts from `hooks/` to `~/.claude/hooks/`:

```
cp hooks/precompact_save.py        ~/.claude/hooks/
cp hooks/session_start_inject.py   ~/.claude/hooks/
cp hooks/stop_hook_checkpoint.py   ~/.claude/hooks/
cp hooks/sse_proxy.py              ~/.claude/hooks/
```

All paths use `Path.home()` and resolve correctly on any platform. The one
constant you may want to change is `PROJECT_STATE` in `session_start_inject.py`
— set it to your project's sticky-note file if it lives somewhere other than
`~/.claude/memory/project_current_state.md`, or `None` to disable resume injection.


## License

MIT — same as continuity v1.

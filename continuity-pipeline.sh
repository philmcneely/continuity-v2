#!/usr/bin/env bash
# continuity-pipeline.sh — sync fleet sessions, reindex, extract facts to fleet memory.
# Designed to run from cron on Kirk every 6 hours.
#
# Usage:
#   ./continuity-pipeline.sh              # full pipeline
#   ./continuity-pipeline.sh sync-only    # just sync + index, no extraction
#   ./continuity-pipeline.sh extract-only # just extraction from existing index

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export CONTINUITY_CENTRAL_STORE="${CONTINUITY_CENTRAL_STORE:-${HOME}/continuity-sessions}"
LOG="${HOME}/Library/Logs/continuity-pipeline.log"
ERRORS=0

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"
}

warn() {
    ERRORS=$((ERRORS + 1))
    log "ERROR: $*"
}

mkdir -p "$CONTINUITY_CENTRAL_STORE"
mkdir -p "$(dirname "$LOG")"

MODE="${1:-full}"

if [[ "$MODE" != "extract-only" ]]; then
    log "=== Sync fleet sessions ==="
    bash "$SCRIPT_DIR/sync-fleet.sh" 2>&1 | tee -a "$LOG" || {
        warn "sync-fleet.sh failed"
    }
fi

if [[ "$MODE" != "extract-only" ]]; then
    log "=== Reindex all nodes ==="
    FOUND_NODES=0
    for node_dir in "$CONTINUITY_CENTRAL_STORE"/*/; do
        if [[ ! -d "$node_dir" ]]; then
            continue
        fi
        FOUND_NODES=$((FOUND_NODES + 1))
        node="$(basename "$node_dir")"
        log "Indexing $node..."
        python3 "$SCRIPT_DIR/index.py" --root "$node_dir" --node "$node" 2>&1 | tee -a "$LOG" || {
            warn "index failed for $node"
        }
    done
    if [[ "$FOUND_NODES" -eq 0 ]]; then
        warn "No node directories found in $CONTINUITY_CENTRAL_STORE — sync may have failed"
    fi
fi

if [[ "$MODE" != "sync-only" ]]; then
    log "=== Extract facts to fleet memory ==="
    python3 "$SCRIPT_DIR/extract-to-fleet-memory.py" --hours 8 2>&1 | tee -a "$LOG" || {
        warn "extraction had errors"
    }
fi

if [[ "$ERRORS" -gt 0 ]]; then
    log "=== Pipeline complete with $ERRORS error(s) ==="
    osascript -e "display notification \"Continuity pipeline: $ERRORS error(s) — check $LOG\" with title \"Continuity Pipeline Failed\"" 2>/dev/null || true
    exit 1
else
    log "=== Pipeline complete ==="
fi

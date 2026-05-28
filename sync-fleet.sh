#!/usr/bin/env bash
# sync-fleet.sh — rsync JSONL sessions from fleet nodes to Mac Studio central store.
# Run from cron on Mac Studio, or manually from any node with SSH access.
#
# Usage:
#   ./sync-fleet.sh                  # sync all configured nodes
#   ./sync-fleet.sh kirk             # sync only one node
#
# After sync, run: python index.py --root /path/to/central/store --node <node>
# Or use the MCP reindex tool (picks up CONTINUITY_ROOT / CONTINUITY_NODE env vars).

set -euo pipefail

CENTRAL_STORE="${CONTINUITY_CENTRAL_STORE:-/Volumes/Data/continuity-sessions}"
CLAUDE_PROJECTS=".claude/projects"

declare -A NODES=(
    [kirk]="localhost"
    [mac-studio]="mac-studio"
)

sync_node() {
    local node="$1"
    local host="${NODES[$node]}"
    local dest="${CENTRAL_STORE}/${node}"

    mkdir -p "$dest"

    if [[ "$host" == "localhost" ]]; then
        rsync -a --include='*/' --include='*.jsonl' --exclude='*' \
            "$HOME/${CLAUDE_PROJECTS}/" "$dest/"
    else
        rsync -az -e "ssh -o ControlMaster=auto -o ControlPersist=60s" \
            --include='*/' --include='*.jsonl' --exclude='*' \
            "${host}:~/${CLAUDE_PROJECTS}/" "$dest/"
    fi

    echo "[sync] $node -> $dest (done)"
}

index_node() {
    local node="$1"
    local dest="${CENTRAL_STORE}/${node}"
    local script_dir
    script_dir="$(cd "$(dirname "$0")" && pwd)"

    if [[ -f "${script_dir}/index.py" ]]; then
        python3 "${script_dir}/index.py" --root "$dest" --node "$node"
    else
        echo "[index] index.py not found in ${script_dir}, skipping"
    fi
}

if [[ $# -gt 0 ]]; then
    target="$1"
    if [[ -z "${NODES[$target]+x}" ]]; then
        echo "Unknown node: $target" >&2
        echo "Available: ${!NODES[*]}" >&2
        exit 1
    fi
    sync_node "$target"
    index_node "$target"
else
    for node in "${!NODES[@]}"; do
        sync_node "$node"
        index_node "$node"
    done
fi

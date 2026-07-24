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

set -uo pipefail   # not -e: one unreachable node must not abort the whole fleet

# Default was /Volumes/Data/continuity-sessions — an external volume that is
# not mounted, so every sync since June wrote nowhere and the pipeline logged
# "1 error" every two hours for a month without anyone noticing.
CENTRAL_STORE="${CONTINUITY_CENTRAL_STORE:-$HOME/continuity-sessions}"
CLAUDE_PROJECTS=".claude/projects"
SSH_KEY="${CONTINUITY_SSH_KEY:-$HOME/git/claude-agent/keys/maxwellsmart}"

# NOTE: plain strings, not `declare -A`. macOS ships bash 3.2, which has no
# associative arrays — the previous version died on its own config block with
# "kirk: unbound variable" before doing any work, on every run since it was
# written. Fields: node|host|ssh_user|remote_home
#
# Only kirk and mac-studio were listed before, so even with working syntax the
# other six nodes were never going to sync.
FLEET="
kirk|localhost|-|-
scotty|192.168.1.41|maxwellsmart|/Users/philmcneely
max|192.168.1.33|maxwellsmart|/home/philmcneely
spock|192.168.1.53|maxwellsmart|/Users/philmcneely
mccoy|192.168.1.52|maxwellsmart|/Users/philmcneely
beelink|192.168.1.10|maxwellsmart|/home/philmcneely
hal9000|192.168.0.225|philmcneely|/home/philmcneely
"

node_field() {
    # node_field <node> <1-based field index>
    echo "$FLEET" | awk -F'|' -v n="$1" -v f="$2" '$1==n {print $f; exit}'
}

node_names() { echo "$FLEET" | awk -F'|' 'NF>1 {print $1}'; }

sync_node() {
    local node="$1"
    local host; host="$(node_field "$node" 2)"
    local dest="${CENTRAL_STORE}/${node}"

    mkdir -p "$dest"

    if [[ "$host" == "localhost" ]]; then
        rsync -a --include='*/' --include='*.jsonl' --exclude='*' \
            "$HOME/${CLAUDE_PROJECTS}/" "$dest/"
    else
        local user; user="$(node_field "$node" 3)"
        local rhome; rhome="$(node_field "$node" 4)"
        # --rsync-path elevates on the far side so maxwellsmart can read files
        # owned by philmcneely. Harmless when already the owner.
        rsync -az \
            -e "ssh -i $SSH_KEY -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o ControlMaster=auto -o ControlPersist=60s" \
            --rsync-path="sudo -u philmcneely rsync" \
            --include='*/' --include='*.jsonl' --exclude='*' \
            "${user}@${host}:${rhome}/${CLAUDE_PROJECTS}/" "$dest/"
    fi

    local rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "[sync] $node FAILED (rc=$rc)" >&2
        return $rc
    fi
    echo "[sync] $node -> $dest ($(find "$dest" -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' ') files)"
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
    if [[ -z "$(node_field "$target" 1)" ]]; then
        echo "Unknown node: $target" >&2
        echo "Available: $(node_names | tr '\n' ' ')" >&2
        exit 1
    fi
    sync_node "$target" && index_node "$target"
else
    failed=0
    for node in $(node_names); do
        # Each node is independent: a box that is off must not stop the rest.
        if sync_node "$node"; then
            index_node "$node" || { echo "[index] $node failed" >&2; failed=$((failed+1)); }
        else
            failed=$((failed+1))
        fi
    done
    [[ $failed -gt 0 ]] && echo "[sync] completed with $failed node failure(s)" >&2
fi
exit 0

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
kirk|192.168.1.77|maxwellsmart|/Users/philmcneely
scotty|192.168.1.41|maxwellsmart|/Users/philmcneely
max|192.168.1.33|maxwellsmart|/home/maxwellsmart
spock|192.168.1.53|maxwellsmart|/Users/philmcneely
mccoy|192.168.1.52|maxwellsmart|/Users/philmcneely
beelink|192.168.1.10|maxwellsmart|/home/philmcneely
hal9000|192.168.0.225|philmcneely|/home/philmcneely
mac-studio|192.168.0.240|philmcneely|/Users/philmcneely
"
# Every fleet node is listed, including ones that mostly serve inference.
# Any box can host a Claude session — you spin one up precisely on the machine
# you are debugging — so a node with no transcripts today is normal, not
# absent. Missing ~/.claude/projects is reported as "no transcripts" rather
# than counted as a failure, so real failures stay visible.

# Whether a node's address belongs to the machine we are running on. Nodes were
# previously identified by the literal string "localhost", which silently meant
# a different machine once this script moved to Mac Studio — Kirk stopped being
# synced and nobody would have noticed until the next time it mattered.
is_local_host() {
    local addr="$1"
    [ "$addr" = "localhost" ] && return 0
    ifconfig 2>/dev/null | grep -qw "$addr" && return 0
    [ "$addr" = "$(hostname -s)" ] && return 0
    return 1
}

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

    # A node with no ~/.claude/projects has simply not hosted a session yet.
    # That is an ordinary state for an inference host, so report it and move on
    # rather than returning a failure that would drown out a real one.
    if is_local_host "$host"; then
        if [ ! -d "$HOME/${CLAUDE_PROJECTS}" ]; then
            echo "[sync] $node — no transcripts (node has not hosted a session)"
            return 0
        fi
    else
        local u; u="$(node_field "$node" 3)"
        local h; h="$(node_field "$node" 4)"
        if ! ssh -i "$SSH_KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=no \
             "${u}@${host}" "test -d '${h}/${CLAUDE_PROJECTS}'" 2>/dev/null; then
            echo "[sync] $node — no transcripts (node has not hosted a session)"
            return 0
        fi
    fi

    if is_local_host "$host"; then
        rsync -a --include='*/' --include='*.jsonl' --exclude='*' \
            "$HOME/${CLAUDE_PROJECTS}/" "$dest/"
    else
        local user; user="$(node_field "$node" 3)"
        local rhome; rhome="$(node_field "$node" 4)"
        # Elevate on the far side only when the SSH user does not already own
        # the transcripts. Max runs Claude Code as maxwellsmart, so sudo-ing to
        # philmcneely there reads a directory that user cannot see and fails
        # with rc=23 — a partial-transfer code that looks like a permissions
        # blip rather than "wrong user entirely".
        # Plain string, not an array: bash 3.2 treats an empty array expanded
        # under `set -u` as an unbound variable, which is the same class of
        # failure as the `declare -A` bug that kept this script from ever
        # running. Word-splitting is safe here — no path in the value.
        local rsync_path=""
        if [ "$rhome" != "/Users/${user}" ] && [ "$rhome" != "/home/${user}" ]; then
            rsync_path="--rsync-path=sudo -u philmcneely rsync"
        fi
        # shellcheck disable=SC2086
        rsync -az \
            -e "ssh -i $SSH_KEY -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o ControlMaster=auto -o ControlPersist=60s" \
            ${rsync_path:+"$rsync_path"} \
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

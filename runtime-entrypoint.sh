#!/bin/sh
set -eu

runtime_uid=10001
runtime_gid=10001
auth_source="${CODEX_AUTH_SOURCE:-}"
codex_home="${CODEX_HOME:-/tmp/etsy-codex-home}"
render_data_dir="${RENDER_DATA_DIR:-}"

auth_file_valid() {
    python3 - "$1" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        value = json.load(handle)
    tokens = value.get("tokens") if isinstance(value, dict) else None
    valid = (
        isinstance(tokens, dict)
        and bool(str(tokens.get("access_token") or "").strip())
        and bool(str(tokens.get("refresh_token") or "").strip())
    )
except (OSError, json.JSONDecodeError, TypeError, ValueError):
    valid = False
sys.exit(0 if valid else 1)
PY
}

auth_digest() {
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
}

sync_auth_from_source() {
    if [ -z "$auth_source" ] || [ ! -f "$auth_source" ] || ! auth_file_valid "$auth_source"; then
        return 1
    fi
    sync_path="$codex_home/.auth.json.sync.$$"
    cp "$auth_source" "$sync_path" || { rm -f "$sync_path"; return 1; }
    chown "$runtime_uid:$runtime_gid" "$sync_path"
    chmod 0600 "$sync_path"
    mv -f "$sync_path" "$codex_home/auth.json"
}

sync_auth_to_source() {
    if [ -z "$auth_source" ] || [ ! -d "$(dirname "$auth_source")" ] || ! auth_file_valid "$codex_home/auth.json"; then
        return 1
    fi
    sync_path="${auth_source}.sync.$$"
    cp "$codex_home/auth.json" "$sync_path" || { rm -f "$sync_path"; return 1; }
    chown 0:0 "$sync_path"
    chmod 0600 "$sync_path"
    mv -f "$sync_path" "$auth_source"
}

auth_sync_loop() {
    last_source_digest="$1"
    last_runtime_digest="$2"
    while :; do
        sleep 2
        current_source_digest="$(auth_digest "$auth_source")"
        current_runtime_digest="$(auth_digest "$codex_home/auth.json")"
        if [ -n "$current_source_digest" ] && [ "$current_source_digest" != "$last_source_digest" ]; then
            if sync_auth_from_source; then
                last_source_digest="$(auth_digest "$auth_source")"
                last_runtime_digest="$(auth_digest "$codex_home/auth.json")"
            fi
            continue
        fi
        if [ -n "$current_runtime_digest" ] && [ "$current_runtime_digest" != "$last_runtime_digest" ]; then
            if sync_auth_to_source; then
                last_source_digest="$(auth_digest "$auth_source")"
                last_runtime_digest="$(auth_digest "$codex_home/auth.json")"
            fi
        fi
    done
}

if [ "$(id -u)" -eq 0 ]; then
    if [ -n "$auth_source" ] && [ ! -r "$auth_source" ] && [ -r /root/.codex/auth.json ]; then
        auth_source=/root/.codex/auth.json
    fi
    mkdir -p "$codex_home"
    # Codex may create its bundled skills and helper files before a restart
    # (for example when an operator runs a read-only status command through
    # Docker as root). Make an existing tree removable while this entrypoint
    # still has its startup capabilities. Re-owning it first would leave the
    # parent directories writable only by UID 10001; with DAC_OVERRIDE removed
    # even root could then fail to remove stale skill files.
    chmod -R a+rwX "$codex_home" 2>/dev/null || true
    chown -R "$runtime_uid:$runtime_gid" "$codex_home"
    chown "$runtime_uid:0" "$codex_home"
    chmod 0770 "$codex_home"
    mkdir -p "$codex_home/skills/.system"
    if [ -d /opt/codex-system-skills/imagegen ]; then
        rm -rf "$codex_home/skills/.system/imagegen"
        cp -R /opt/codex-system-skills/imagegen "$codex_home/skills/.system/imagegen"
        chown -R "$runtime_uid:$runtime_gid" "$codex_home/skills/.system/imagegen"
    fi
    # app-server refreshes the system-skill tree on startup.  Its parent
    # directories must be writable by the dropped-privilege renderer user;
    # otherwise the refresh removes the vendored skill and fails closed.
    chown -R "$runtime_uid:$runtime_gid" "$codex_home/skills"
    auth_sync_pid=""
    if [ -n "$auth_source" ] && sync_auth_from_source; then
        auth_sync_loop "$(auth_digest "$auth_source")" "$(auth_digest "$codex_home/auth.json")" &
        auth_sync_pid=$!
    else
        echo "Codex auth source is not a valid readable regular file; refusing customer-facing renders." >&2
    fi
    if [ -n "$render_data_dir" ] && [ -d "$render_data_dir" ]; then
        chown -R "$runtime_uid:$runtime_gid" "$render_data_dir"
    fi
    setpriv \
        --reuid="$runtime_uid" \
        --regid="$runtime_gid" \
        --clear-groups \
        --bounding-set=-all \
        --inh-caps=-all \
        --ambient-caps=-all \
        --nnp \
        "$@" &
    application_pid=$!
    stop_children() {
        kill "$application_pid" 2>/dev/null || true
        if [ -n "$auth_sync_pid" ]; then
            kill "$auth_sync_pid" 2>/dev/null || true
        fi
    }
    trap stop_children TERM INT HUP
    if wait "$application_pid"; then
        application_status=0
    else
        application_status=$?
    fi
    if [ -n "$auth_sync_pid" ]; then
        kill "$auth_sync_pid" 2>/dev/null || true
        wait "$auth_sync_pid" 2>/dev/null || true
    fi
    exit "$application_status"
fi

exec "$@"

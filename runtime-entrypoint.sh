#!/bin/sh
set -eu

runtime_uid=10001
runtime_gid=10001
auth_source="${CODEX_AUTH_SOURCE:-}"
codex_home="${CODEX_HOME:-/tmp/etsy-codex-home}"
render_data_dir="${RENDER_DATA_DIR:-}"

if [ "$(id -u)" -eq 0 ]; then
    if [ -n "$auth_source" ] && [ ! -r "$auth_source" ] && [ -r /root/.codex/auth.json ]; then
        auth_source=/root/.codex/auth.json
    fi
    mkdir -p "$codex_home"
    chown "$runtime_uid:0" "$codex_home"
    chmod 0770 "$codex_home"
    if [ -n "$auth_source" ]; then
        test -r "$auth_source"
        cp "$auth_source" "$codex_home/auth.json"
        chown "$runtime_uid:$runtime_gid" "$codex_home/auth.json"
        chmod 0600 "$codex_home/auth.json"
    fi
    if [ -n "$render_data_dir" ] && [ -d "$render_data_dir" ]; then
        chown -R "$runtime_uid:$runtime_gid" "$render_data_dir"
    fi
    exec setpriv \
        --reuid="$runtime_uid" \
        --regid="$runtime_gid" \
        --clear-groups \
        --bounding-set=-all \
        --inh-caps=-all \
        --ambient-caps=-all \
        --nnp \
        "$@"
fi

exec "$@"

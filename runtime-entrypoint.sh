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
    if [ -n "$auth_source" ]; then
        if [ -f "$auth_source" ] && [ -r "$auth_source" ]; then
            cp "$auth_source" "$codex_home/auth.json"
            chown "$runtime_uid:$runtime_gid" "$codex_home/auth.json"
            chmod 0600 "$codex_home/auth.json"
        else
            echo "Codex auth source is not a readable regular file; continuing so the configured API fallback can serve renders." >&2
        fi
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

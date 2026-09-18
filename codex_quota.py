"""Authoritative Codex quota preflight for the Etsy renderer.

The Codex app-server exposes ``account/rateLimits/read``, a structured
rate-limit snapshot.  This module turns that snapshot into one of three stable
states:

* ``available``  - included Codex image capacity may be used.
* ``exhausted``  - positive, structured evidence that Codex usage is spent.
* ``unknown``    - the signal could not be read or understood.

Policy (see ``etsy-automation`` decisions): ``exhausted`` never authorizes paid
OpenAI API spend by itself, and ``unknown`` is never treated as exhaustion nor
allowed to authorize paid spend.  Only an explicit operator authorization may
do that, and that decision lives in ``openai_fallback.paid_fallback_authorized``.

This module is intentionally independent of the network provider so it can be
unit tested without any billable call.  It never invokes any image model.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import threading
import time
from typing import Any, Callable

STATUS_AVAILABLE = "available"
STATUS_EXHAUSTED = "exhausted"
STATUS_UNKNOWN = "unknown"

QUOTA_SOURCE = "codex_app_server_account_rateLimits_read"
DEFAULT_QUOTA_TIMEOUT_SECONDS = 20
DEFAULT_QUOTA_CACHE_SECONDS = 60
_MAX_DETAIL_CHARS = 400

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"at": 0.0, "value": None}


def _bounded_timeout(timeout: float | int | None) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        value = float(DEFAULT_QUOTA_TIMEOUT_SECONDS)
    return max(3.0, min(value, 60.0))


def _pct(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().rstrip("%")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _balance_is_zero(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return float(value) == 0.0
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text) == 0.0
        except ValueError:
            return False
    return False


def _window_used_percent(window: Any) -> float | None:
    if not isinstance(window, dict):
        return None
    return _pct(window.get("usedPercent", window.get("used_percent")))


def classify_rate_limit_payload(payload: Any) -> dict[str, Any]:
    """Classify a raw ``account/rateLimits/read`` JSON-RPC response.

    Accepts either the full JSON-RPC envelope (``{"result": {...}}``) or the
    result object directly.  Returns a stable structure with ``status`` one of
    ``available``/``exhausted``/``unknown`` plus bounded, non-secret evidence.
    """
    result = payload.get("result") if isinstance(payload, dict) and isinstance(payload.get("result"), dict) else payload
    if not isinstance(result, dict):
        return _unknown("rate_limit_payload_missing")

    limits = result.get("rateLimits") if isinstance(result.get("rateLimits"), dict) else None
    if limits is None:
        by_id = result.get("rateLimitsByLimitId")
        if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
            limits = by_id["codex"]
    if not isinstance(limits, dict):
        return _unknown("rate_limit_snapshot_missing")

    primary = _window_used_percent(limits.get("primary"))
    secondary = _window_used_percent(limits.get("secondary"))
    credits = limits.get("credits") if isinstance(limits.get("credits"), dict) else {}
    has_credits = credits.get("hasCredits", credits.get("has_credits"))
    unlimited = bool(credits.get("unlimited"))
    balance = credits.get("balance")
    ordinary = result.get("ordinaryUsageAllowed", result.get("ordinary_usage_allowed"))
    reached = limits.get("rateLimitReachedType", limits.get("rate_limit_reached_type"))

    evidence = {
        "source": QUOTA_SOURCE,
        "plan_type": str(limits.get("planType") or limits.get("plan_type") or ""),
        "rate_limit_reached_type": str(reached or ""),
        "ordinary_usage_allowed": ordinary if isinstance(ordinary, bool) else None,
        "primary_used_percent": primary,
        "secondary_used_percent": secondary,
        "credits_has_credits": has_credits if isinstance(has_credits, bool) else None,
        "credits_unlimited": unlimited,
        "credits_balance_zero": _balance_is_zero(balance),
        "limit_id": str(limits.get("limitId") or limits.get("limit_id") or ""),
    }

    # Positive exhaustion evidence only.
    if ordinary is False:
        return _result(STATUS_EXHAUSTED, "ordinary_usage_disallowed", evidence)
    if reached:
        return _result(STATUS_EXHAUSTED, "rate_limit_reached", evidence)
    if unlimited or has_credits is True:
        return _result(STATUS_AVAILABLE, "credits_available", evidence)

    windows = [value for value in (primary, secondary) if value is not None]
    if windows and max(windows) >= 100.0:
        return _result(STATUS_EXHAUSTED, "rate_limit_window_exhausted", evidence)
    if windows:
        return _result(STATUS_AVAILABLE, "rate_limit_window_available", evidence)
    if has_credits is False:
        return _result(STATUS_EXHAUSTED, "no_credits_no_window", evidence)
    return _unknown("rate_limit_snapshot_inconclusive", evidence)


def _result(status: str, reason: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "status": status,
        "reason": reason,
        "source": QUOTA_SOURCE,
        "checked_at": time.time(),
    }
    if evidence:
        payload.update(evidence)
    return payload


def _unknown(reason: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return _result(STATUS_UNKNOWN, reason, evidence)


def _read_rate_limits(command: list[str], timeout: float) -> dict[str, Any]:
    """Query the Codex app-server for a structured rate-limit snapshot."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    assert process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers: dict[int, bytes] = {}
    deadline = time.time() + timeout

    def send(message: dict[str, Any]) -> None:
        if process.stdin is None:
            raise OSError("codex_quota_stdin_missing")
        process.stdin.write((json.dumps(message, ensure_ascii=True) + "\n").encode())
        process.stdin.flush()

    def read_until(target_id: int) -> dict[str, Any] | None:
        while time.time() < deadline:
            for key, _ in selector.select(min(0.5, max(0.05, deadline - time.time()))):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    continue
                fd = key.fileobj.fileno()
                buffers[fd] = buffers.get(fd, b"") + data
                while b"\n" in buffers[fd]:
                    line, buffers[fd] = buffers[fd].split(b"\n", 1)
                    rendered = line.decode("utf-8", errors="replace").strip()
                    if not rendered:
                        continue
                    try:
                        event = json.loads(rendered)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(event, dict) and event.get("id") == target_id:
                        return event
            if process.poll() is not None:
                break
        return None

    try:
        send({
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {"name": "etsy-codex-renderer-quota", "title": "Etsy Codex renderer quota", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        })
        if not read_until(1):
            return _unknown("quota_probe_initialize_failed")
        send({"method": "initialized"})
        send({"method": "account/rateLimits/read", "id": 2, "params": {}})
        response = read_until(2)
        if response is None:
            return _unknown("quota_probe_timeout")
        if response.get("error"):
            detail = " ".join(str(response.get("error")).split())[:_MAX_DETAIL_CHARS]
            return _unknown(f"quota_probe_error:{detail}" if detail else "quota_probe_error")
        return classify_rate_limit_payload(response)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return _unknown(f"quota_probe_transport_failed:{type(exc).__name__}")
    finally:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.communicate()
            except (OSError, ValueError):
                pass
        selector.close()


def probe(
    command: list[str] | None = None,
    *,
    timeout: float | int | None = None,
    cache_seconds: float | int | None = None,
    reader: Callable[[list[str], float], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the current Codex quota state with a short bounded cache.

    ``reader`` is injectable for deterministic tests; it must return a
    classification dict.  A reader failure is mapped to ``unknown`` and never to
    exhaustion.
    """
    resolved_command = list(command or [])
    if not resolved_command:
        return _unknown("quota_probe_command_missing")
    window = DEFAULT_QUOTA_CACHE_SECONDS if cache_seconds is None else float(cache_seconds)
    bounded_timeout = _bounded_timeout(timeout)
    if window > 0:
        now = time.time()
        with _CACHE_LOCK:
            cached = _CACHE.get("value")
            if isinstance(cached, dict) and now - float(_CACHE.get("at") or 0.0) < window:
                return dict(cached)
    active_reader = reader or _read_rate_limits
    try:
        classification = active_reader(resolved_command, bounded_timeout)
    except Exception as exc:  # noqa: BLE001 - a probe failure must fail safe.
        classification = _unknown(f"quota_probe_reader_failed:{type(exc).__name__}")
    if not isinstance(classification, dict) or "status" not in classification:
        classification = _unknown("quota_probe_invalid_classification")
    if window > 0:
        with _CACHE_LOCK:
            _CACHE["at"] = time.time()
            _CACHE["value"] = dict(classification)
    return dict(classification)


def reset_cache() -> None:
    with _CACHE_LOCK:
        _CACHE["at"] = 0.0
        _CACHE["value"] = None

from __future__ import annotations

import pytest

import codex_quota


def test_capability_version_payload_is_exhausted():
    payload = {
        "result": {
            "ordinaryUsageAllowed": False,
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 0, "windowDurationMins": 300},
                "secondary": {"usedPercent": 100, "windowDurationMins": 10080},
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                "planType": "plus",
                "rateLimitReachedType": "rate_limit_reached",
            },
        },
    }
    result = codex_quota.classify_rate_limit_payload(payload)
    assert result["status"] == codex_quota.STATUS_EXHAUSTED
    assert result["reason"] == "ordinary_usage_disallowed"
    assert result["secondary_used_percent"] == 100.0
    assert result["source"] == codex_quota.QUOTA_SOURCE


def test_legacy_version_payload_without_ordinary_flag_is_exhausted():
    payload = {
        "result": {
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 0},
                "secondary": {"usedPercent": 100},
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                "rateLimitReachedType": "rate_limit_reached",
            },
        },
    }
    result = codex_quota.classify_rate_limit_payload(payload)
    assert result["status"] == codex_quota.STATUS_EXHAUSTED
    assert result["rate_limit_reached_type"] == "rate_limit_reached"


def test_window_below_full_is_available():
    payload = {"result": {"rateLimits": {
        "primary": {"usedPercent": 10}, "secondary": {"usedPercent": 40},
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
    }}}
    result = codex_quota.classify_rate_limit_payload(payload)
    assert result["status"] == codex_quota.STATUS_AVAILABLE


def test_ordinary_usage_allowed_true_is_available():
    payload = {"result": {
        "ordinaryUsageAllowed": True,
        "rateLimits": {"primary": {"usedPercent": 99}, "secondary": {"usedPercent": 99}},
    }}
    result = codex_quota.classify_rate_limit_payload(payload)
    assert result["status"] == codex_quota.STATUS_AVAILABLE


def test_unlimited_credits_are_available_even_at_full_window():
    payload = {"result": {"rateLimits": {
        "primary": {"usedPercent": 100},
        "credits": {"hasCredits": False, "unlimited": True, "balance": "0"},
        "rateLimitReachedType": None,
    }}}
    result = codex_quota.classify_rate_limit_payload(payload)
    assert result["status"] == codex_quota.STATUS_AVAILABLE
    assert result["reason"] == "credits_available"


@pytest.mark.parametrize("payload", [
    None,
    {},
    {"result": {}},
    {"result": {"rateLimits": "nonsense"}},
    {"result": {"rateLimits": {"primary": {}, "secondary": {}}}},
])
def test_inconclusive_payloads_are_unknown(payload):
    result = codex_quota.classify_rate_limit_payload(payload)
    assert result["status"] == codex_quota.STATUS_UNKNOWN


def test_probe_uses_injected_reader_and_caches():
    codex_quota.reset_cache()
    calls = {"count": 0}

    def reader(command, timeout):
        calls["count"] += 1
        return {"status": "available", "reason": "ok", "source": codex_quota.QUOTA_SOURCE}

    first = codex_quota.probe(["codex"], cache_seconds=60, reader=reader)
    second = codex_quota.probe(["codex"], cache_seconds=60, reader=reader)
    assert first["status"] == "available"
    assert second["status"] == "available"
    assert calls["count"] == 1


def test_probe_reader_failure_is_unknown_and_never_exhausted():
    codex_quota.reset_cache()

    def reader(command, timeout):
        raise RuntimeError("boom")

    result = codex_quota.probe(["codex"], cache_seconds=0, reader=reader)
    assert result["status"] == codex_quota.STATUS_UNKNOWN


def test_probe_invalid_reader_result_is_unknown():
    codex_quota.reset_cache()
    result = codex_quota.probe(["codex"], cache_seconds=0, reader=lambda command, timeout: {"nope": True})
    assert result["status"] == codex_quota.STATUS_UNKNOWN


def test_probe_without_command_is_unknown():
    result = codex_quota.probe([], cache_seconds=0)
    assert result["status"] == codex_quota.STATUS_UNKNOWN
    assert result["reason"] == "quota_probe_command_missing"


def test_status_constants_are_stable():
    assert codex_quota.STATUS_AVAILABLE == "available"
    assert codex_quota.STATUS_EXHAUSTED == "exhausted"
    assert codex_quota.STATUS_UNKNOWN == "unknown"

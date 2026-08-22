import pytest

import openai_fallback


@pytest.mark.parametrize(
    "raw",
    [
        "insufficient_quota",
        "quota exhausted",
        "quota exceeded",
        "out of quota",
        "usage limit reached",
        "you've hit your usage limit",
        "weekly limit reached",
        "plan limit exceeded",
        "x-codex-primary-used-percent: 100; x-codex-credits-has-credits: false",
        "x-codex-primary-used-percent=100.0 x-codex-credits-balance=0",
        "quota remaining: 0",
        "quota left = 0.0",
        "weighted tokens left: 0",
        "quota available: false",
        "quota available = 0",
    ],
)
def test_codex_quota_exhausted_requires_positive_exhaustion_evidence(raw):
    assert openai_fallback.codex_quota_exhausted(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "request timed out",
        "network connection reset",
        "429 rate limit exceeded",
        "500 upstream capacity",
        "401 unauthorized",
        "usage limit: 100000 requests",
        "weekly limit resets Monday",
        "plan limit: premium",
        "quota available: true",
        "quota available: 50",
        "quota remaining: 100",
        "weighted tokens left: 42",
        "x-codex-primary-used-percent: 99; x-codex-credits-has-credits: false",
        "x-codex-primary-used-percent: 100; x-codex-credits-has-credits: true; x-codex-credits-balance: 5",
        "malformed provider result",
    ],
)
def test_codex_quota_exhausted_rejects_nonexhausted_and_nonquota_failures(raw):
    assert openai_fallback.codex_quota_exhausted(raw) is False

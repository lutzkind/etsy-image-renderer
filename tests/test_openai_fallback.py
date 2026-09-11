import base64
from pathlib import Path

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


def test_responses_fallback_forces_the_only_hosted_image_tool(monkeypatch, tmp_path):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "output": [{
                    "type": "image_generation_call",
                    "result": base64.b64encode(b"\x89PNG\r\n\x1a\nresult").decode("ascii"),
                }],
            }

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, *, headers, json):
            captured.update({"url": url, "headers": headers, "payload": json})
            return FakeResponse()

    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.setattr(openai_fallback.httpx, "Client", FakeClient)
    input_path = Path(tmp_path) / "art.png"
    input_path.write_bytes(b"\x89PNG\r\n\x1a\ninput")

    data, mime, model = openai_fallback.generate_image("make a frame", [input_path], 120)

    assert data.startswith(b"\x89PNG")
    assert mime == "image/png"
    assert model == "gpt-image-2"
    assert captured["payload"]["tool_choice"] == "required"
    assert captured["payload"]["tools"][0]["type"] == "image_generation"
    assert captured["payload"]["tools"][0]["action"] == "edit"


def test_responses_fallback_preserves_bounded_provider_error_detail(monkeypatch):
    class FakeResponse:
        status_code = 400
        text = ""

        def json(self):
            return {"error": {"type": "invalid_request_error", "code": "invalid_value", "message": "bad tool choice"}}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.setattr(openai_fallback.httpx, "Client", lambda **kwargs: FakeClient())
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match="http_400:invalid_request_error:invalid_value:bad tool choice"):
        openai_fallback.generate_image("make a frame", [], 120)

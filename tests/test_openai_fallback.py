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
    monkeypatch.setenv("ALLOW_PAID_OPENAI_IMAGE_FALLBACK", "true")
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
    # gpt-image-2 rejects input_fidelity; it must never be sent.
    assert "input_fidelity" not in captured["payload"]["tools"][0]


def test_generation_request_omits_edit_only_parameters(monkeypatch, tmp_path):
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
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, *, headers, json):
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.setenv("ALLOW_PAID_OPENAI_IMAGE_FALLBACK", "true")
    monkeypatch.setattr(openai_fallback.httpx, "Client", lambda **kwargs: FakeClient())

    openai_fallback.generate_image("make a frame", [], 120)

    tool = captured["payload"]["tools"][0]
    assert tool["action"] == "generate"
    assert "input_fidelity" not in tool


def test_gpt_image_1_reference_edit_keeps_input_fidelity(monkeypatch, tmp_path):
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
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, *, headers, json):
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.setenv("ALLOW_PAID_OPENAI_IMAGE_FALLBACK", "true")
    monkeypatch.setenv("OPENAI_IMAGE_FALLBACK_IMAGE_MODEL", "gpt-image-1")
    monkeypatch.setattr(openai_fallback.httpx, "Client", lambda **kwargs: FakeClient())
    input_path = Path(tmp_path) / "art.png"
    input_path.write_bytes(b"\x89PNG\r\n\x1a\ninput")

    openai_fallback.generate_image("edit this", [input_path], 120)

    tool = captured["payload"]["tools"][0]
    assert tool["action"] == "edit"
    assert tool["input_fidelity"] == "high"


def test_unsupported_image_model_fails_closed_without_request(monkeypatch):
    sent = []
    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.setenv("ALLOW_PAID_OPENAI_IMAGE_FALLBACK", "true")
    monkeypatch.setenv("OPENAI_IMAGE_FALLBACK_IMAGE_MODEL", "gpt-image-unknown")
    monkeypatch.setattr(openai_fallback.httpx, "Client", lambda **kwargs: sent.append(kwargs))
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match="unsupported_image_model"):
        openai_fallback.generate_image("make a frame", [], 120)
    assert sent == []


def test_paid_fallback_is_disabled_by_default(monkeypatch):
    sent = []
    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.delenv("ALLOW_PAID_OPENAI_IMAGE_FALLBACK", raising=False)
    monkeypatch.setattr(openai_fallback.httpx, "Client", lambda **kwargs: sent.append(kwargs))
    assert openai_fallback.paid_fallback_authorized() is False
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match="not_authorized"):
        openai_fallback.generate_image("make a frame", [], 120)
    assert sent == []


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", "enabled"])
def test_paid_fallback_authorization_truthy_values(monkeypatch, value):
    monkeypatch.setenv("ALLOW_PAID_OPENAI_IMAGE_FALLBACK", value)
    assert openai_fallback.paid_fallback_authorized() is True


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "disabled", "maybe"])
def test_paid_fallback_authorization_falsy_values(monkeypatch, value):
    monkeypatch.setenv("ALLOW_PAID_OPENAI_IMAGE_FALLBACK", value)
    assert openai_fallback.paid_fallback_authorized() is False


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
    monkeypatch.setenv("ALLOW_PAID_OPENAI_IMAGE_FALLBACK", "true")
    monkeypatch.setattr(openai_fallback.httpx, "Client", lambda **kwargs: FakeClient())
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match="http_400:invalid_request_error:invalid_value:bad tool choice"):
        openai_fallback.generate_image("make a frame", [], 120)


def _authorized(monkeypatch, status=200, payload=None, post=None):
    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.setenv("ALLOW_PAID_OPENAI_IMAGE_FALLBACK", "true")

    class FakeResponse:
        status_code = status
        text = ""

        def json(self):
            if payload is None:
                raise ValueError("no json")
            return payload

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            if post is not None:
                return post()
            return FakeResponse()

    monkeypatch.setattr(openai_fallback.httpx, "Client", lambda **kwargs: FakeClient())
    return FakeClient()


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 502, 503])
def test_http_error_statuses_are_surfaced(monkeypatch, status):
    _authorized(monkeypatch, status=status, payload={"error": {"type": "t", "code": "c", "message": "m"}})
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match=f"http_{status}"):
        openai_fallback.generate_image("p", [], 120)


def test_transport_timeout_is_surfaced(monkeypatch):
    def boom():
        raise openai_fallback.httpx.ConnectTimeout("timed out")

    _authorized(monkeypatch, post=boom)
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match="transport_failed"):
        openai_fallback.generate_image("p", [], 120)


def test_malformed_json_is_surfaced(monkeypatch):
    _authorized(monkeypatch, payload=None)
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match="invalid_json"):
        openai_fallback.generate_image("p", [], 120)


def test_missing_output_is_surfaced(monkeypatch):
    _authorized(monkeypatch, payload={"output": []})
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match="output_missing"):
        openai_fallback.generate_image("p", [], 120)


def test_ambiguous_output_is_surfaced(monkeypatch):
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nx").decode("ascii")
    _authorized(monkeypatch, payload={"output": [
        {"type": "image_generation_call", "result": encoded},
        {"type": "image_generation_call", "result": encoded},
    ]})
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match="output_ambiguous"):
        openai_fallback.generate_image("p", [], 120)


def test_invalid_base64_result_is_surfaced(monkeypatch):
    _authorized(monkeypatch, payload={"output": [
        {"type": "image_generation_call", "result": "!!!not-base64!!!"},
    ]})
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match="invalid_base64"):
        openai_fallback.generate_image("p", [], 120)


def test_oversized_result_is_surfaced(monkeypatch):
    oversized = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * (openai_fallback.MAX_FALLBACK_OUTPUT_BYTES + 1)).decode("ascii")
    _authorized(monkeypatch, payload={"output": [
        {"type": "image_generation_call", "result": oversized},
    ]})
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match="output_too_large"):
        openai_fallback.generate_image("p", [], 120)


def test_unsupported_result_bytes_are_surfaced(monkeypatch):
    not_an_image = base64.b64encode(b"not-an-image").decode("ascii")
    _authorized(monkeypatch, payload={"output": [
        {"type": "image_generation_call", "result": not_an_image},
    ]})
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match="unsupported_output"):
        openai_fallback.generate_image("p", [], 120)


def test_transport_failure_does_not_poison_retry(monkeypatch):
    attempts = {"count": 0}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"output": [{
                "type": "image_generation_call",
                "result": base64.b64encode(b"\x89PNG\r\n\x1a\nresult").decode("ascii"),
            }]}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise openai_fallback.httpx.ReadTimeout("first")
            return FakeResponse()

    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.setenv("ALLOW_PAID_OPENAI_IMAGE_FALLBACK", "true")
    monkeypatch.setattr(openai_fallback.httpx, "Client", lambda **kwargs: FakeClient())
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match="transport_failed"):
        openai_fallback.generate_image("p", [], 120)
    data, mime, model = openai_fallback.generate_image("p", [], 120)
    assert data.startswith(b"\x89PNG")
    assert attempts["count"] == 2

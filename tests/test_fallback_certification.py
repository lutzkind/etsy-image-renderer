"""Zero-cost certification of the OpenAI image fallback.

Policy forbids real paid OpenAI image generation during repair sessions.  This
harness therefore exercises the actual production request-construction and
response-handling code while mocking only the network/provider boundary.

It proves:

* explicit paid authorization + Codex unavailable -> a correctly formed
  ``gpt-image-2`` image-generation request, a mocked valid provider raster, and
  a result the gallery path can consume; and
* production default (no authorization) + Codex unavailable -> the paid
  fallback code is never invoked.

The label distinguishes "implementation verified" from "real paid provider
invocation not performed by policy".
"""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as renderer
import openai_fallback

PNG_INPUT = b"\x89PNG\r\n\x1a\ninput-artwork"
PNG_RESULT = b"\x89PNG\r\n\x1a\n" + b"mock-provider-result"
AUTH = {"Authorization": "Bearer secret"}


class _FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"output": [{
            "type": "image_generation_call",
            "result": base64.b64encode(PNG_RESULT).decode("ascii"),
        }]}


class _FakeClient:
    def __init__(self, captured):
        self._captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, *, headers, json):
        self._captured.update({"url": url, "headers": headers, "payload": json})
        return _FakeResponse()


def test_authorized_fallback_is_well_formed_and_consumable(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.setenv("ALLOW_PAID_OPENAI_IMAGE_FALLBACK", "true")
    monkeypatch.setattr(openai_fallback.httpx, "Client", lambda **kwargs: _FakeClient(captured))
    source = Path(tmp_path) / "source.png"
    source.write_bytes(PNG_INPUT)

    data, mime, model = openai_fallback.generate_image("render the lifestyle scene", [source], 120)

    # Request contract: GPT Image 2 reference edit without the unsupported param.
    tool = captured["payload"]["tools"][0]
    assert captured["url"].endswith("/responses")
    assert tool == {
        "type": "image_generation",
        "model": "gpt-image-2",
        "action": "edit",
        "quality": "high",
        "size": "auto",
        "output_format": "png",
    }
    assert len(captured["payload"]["input"][0]["content"]) == 2
    assert captured["payload"]["input"][0]["content"][1]["type"] == "input_image"

    # Response handling: a distinct, valid raster is accepted and gallery-safe.
    assert data == PNG_RESULT
    assert mime == "image/png"
    assert model == "gpt-image-2"
    digest = renderer._reject_reused_input_raster(data, {hashlib.sha256(PNG_INPUT).hexdigest()})
    assert digest == hashlib.sha256(PNG_RESULT).hexdigest()
    assert renderer._sniff_image(data)[0] == "image/png"


def test_default_production_never_invokes_paid_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.delenv("ALLOW_PAID_OPENAI_IMAGE_FALLBACK", raising=False)
    monkeypatch.setattr(renderer, "_validate_public_https_url", lambda value: value)
    monkeypatch.setattr(renderer, "readiness", lambda: {"ready": True})
    fallback_calls = []

    def fake_download(url, target):
        path = target.with_suffix(".png")
        path.write_bytes(PNG_INPUT)
        return path

    def quota_run(workspace, inputs, prompt, timeout):
        return renderer._CodexRun(1, "", "insufficient_quota: usage limit reached", ())

    monkeypatch.setattr(renderer, "_download_image", fake_download)
    monkeypatch.setattr(renderer, "_run_codex_app_server", quota_run)
    monkeypatch.setattr(openai_fallback, "generate_image", lambda *args: fallback_calls.append(args))

    response = TestClient(renderer.app).post(
        "/render", headers=AUTH,
        json={"mode": "minimal_frame", "input_urls": ["https://example.com/art.jpg"]},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "codex_quota_unavailable"
    assert fallback_calls == []
    assert openai_fallback.paid_fallback_authorized() is False

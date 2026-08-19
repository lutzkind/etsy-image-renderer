from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as renderer
import openai_fallback


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"fallback-test"


@pytest.fixture(autouse=True)
def reset_fallback(monkeypatch):
    openai_fallback.reset_quota_circuit()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    yield
    openai_fallback.reset_quota_circuit()


def test_quota_detection_is_strict():
    assert openai_fallback.codex_quota_exhausted("You've hit your usage limit")
    assert openai_fallback.codex_quota_exhausted("insufficient_quota")
    assert openai_fallback.codex_quota_exhausted("weekly limit reached")
    assert not openai_fallback.codex_quota_exhausted("429 Too Many Requests")
    assert not openai_fallback.codex_quota_exhausted("temporary rate limit")


def test_quota_circuit_opens_for_configured_window(monkeypatch):
    monkeypatch.setenv("CODEX_QUOTA_CIRCUIT_SECONDS", "60")
    assert not openai_fallback.quota_circuit_open(now=100)
    assert openai_fallback.mark_codex_quota_exhausted(now=100) == 160
    assert openai_fallback.quota_circuit_open(now=159)
    assert not openai_fallback.quota_circuit_open(now=160)


def test_generate_image_uses_responses_image_tool(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(_png())
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    seen = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "output": [
                    {
                        "type": "image_generation_call",
                        "result": base64.b64encode(_png()).decode("ascii"),
                    }
                ]
            }

    class FakeClient:
        def __init__(self, timeout):
            seen["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers, json):
            seen["url"] = url
            seen["headers"] = headers
            seen["json"] = json
            return FakeResponse()

    monkeypatch.setattr(openai_fallback.httpx, "Client", FakeClient)
    data, mime, model = openai_fallback.generate_image("render this", [source], 120)

    assert data == _png()
    assert mime == "image/png"
    assert model == "gpt-image-2"
    assert seen["url"].endswith("/responses")
    assert seen["headers"]["Authorization"] == "Bearer test-key"
    assert seen["json"]["tool_choice"] == {"type": "image_generation"}
    tool = seen["json"]["tools"][0]
    assert tool["type"] == "image_generation"
    assert tool["model"] == "gpt-image-2"
    assert tool["action"] == "edit"
    assert tool["input_fidelity"] == "high"
    assert seen["json"]["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,")
    assert "test-key" not in json.dumps(seen["json"])


def test_open_quota_circuit_bypasses_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "configured")
    monkeypatch.setattr(openai_fallback, "quota_circuit_open", lambda: True)
    called = {}

    def fake_generate(prompt, inputs, timeout):
        called["prompt"] = prompt
        called["inputs"] = inputs
        called["timeout"] = timeout
        return _png(), "image/png", "gpt-image-2"

    monkeypatch.setattr(openai_fallback, "generate_image", fake_generate)
    run = renderer._run_codex_app_server(
        tmp_path,
        [],
        "$imagegen\nDo not call an external image API, generate one image. Return only a brief confirmation after generating the image.",
        60,
    )

    assert run.returncode == 0
    assert run.stderr == "openai_api_fallback"
    assert run.saved_paths[0].read_bytes() == _png()
    assert "image_generation_call" in run.stdout
    assert "external image API" not in called["prompt"]
    assert "brief confirmation" not in called["prompt"]


def test_missing_codex_auth_uses_configured_api_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "configured")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    called = {}

    def fake_generate(prompt, inputs, timeout):
        called["prompt"] = prompt
        called["inputs"] = inputs
        called["timeout"] = timeout
        return _png(), "image/png", "gpt-image-2"

    monkeypatch.setattr(openai_fallback, "generate_image", fake_generate)
    run = renderer._run_codex_app_server(tmp_path, [], "$imagegen\nGenerate one image.", 60)

    assert run.returncode == 0
    assert run.stderr == "openai_api_fallback"
    assert run.saved_paths[0].read_bytes() == _png()
    assert "image_generation_call" in run.stdout
    assert called["timeout"] == 60


def test_api_fallback_requires_key(tmp_path):
    run = renderer._run_openai_api_fallback(tmp_path, [], "render", 60)
    assert run.returncode == 1
    assert "openai_image_fallback_not_configured" in run.stderr

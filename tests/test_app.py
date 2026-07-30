from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as renderer

PNG = b"\x89PNG\r\n\x1a\n" + b"test"


def test_private_urls_are_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="input_url_not_public"):
        renderer._validate_public_https_url("https://example.test/a.png")


def test_fixed_mode_input_counts():
    assert renderer.EXPECTED_INPUTS == {"minimal_frame": 1, "lifestyle": 2, "orientation": 1}


def test_prompt_forbids_direct_api_fallback():
    prompt = renderer._prompt("lifestyle", "pet portrait")
    assert "built-in image_gen/image_generation tool exactly once" in prompt
    assert "Do not call an external image API" in prompt
    assert "Image 2" in prompt


def test_command_enables_image_generation(tmp_path):
    command = renderer._codex_command(tmp_path, [tmp_path / "a.png"])
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("--enable") + 1] == "image_generation"
    assert "--ephemeral" in command
    assert command[-1] == "-"


def test_render_endpoint_requires_auth(monkeypatch):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    client = TestClient(renderer.app)
    response = client.post("/render", json={"mode": "orientation", "input_urls": ["https://example.com/a.png"]})
    assert response.status_code == 401


def test_render_returns_one_mocked_image(tmp_path, monkeypatch):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("RENDER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(renderer, "_validate_public_https_url", lambda value: value)
    monkeypatch.setattr(renderer, "readiness", lambda: {"ready": True})

    def fake_download(url: str, target: Path) -> Path:
        path = target.with_suffix(".png")
        path.write_bytes(PNG)
        return path

    def fake_run(*args, **kwargs):
        output = Path(renderer._codex_home()) / "generated_images" / "result.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(PNG + b"output")
        return subprocess.CompletedProcess(["codex"], 0, stdout="ok", stderr="")

    monkeypatch.setattr(renderer, "_download_image", fake_download)
    monkeypatch.setattr(subprocess, "run", fake_run)
    client = TestClient(renderer.app)
    response = client.post(
        "/render",
        headers={"Authorization": "Bearer secret"},
        json={"mode": "orientation", "input_urls": ["https://example.com/a.png"]},
    )
    assert response.status_code == 200
    assert response.content == PNG + b"output"
    assert response.headers["x-renderer"] == "codex-local"

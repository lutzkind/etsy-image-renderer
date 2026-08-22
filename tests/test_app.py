from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as renderer

PNG = b"\x89PNG\r\n\x1a\n" + b"test"
AUTH = {"Authorization": "Bearer secret"}


@pytest.fixture(autouse=True)
def clear_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER_DATA_DIR", str(tmp_path / "render-data"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    renderer._REQUEST_DIGESTS.clear()
    renderer._ASYNC_JOBS.clear()
    renderer._ASYNC_HASH_INDEX.clear()
    renderer._ASYNC_QUEUE_IDS.clear()
    renderer._ASYNC_STATE_RESTORED = False
    yield
    renderer._REQUEST_DIGESTS.clear()
    renderer._ASYNC_JOBS.clear()
    renderer._ASYNC_HASH_INDEX.clear()
    renderer._ASYNC_QUEUE_IDS.clear()
    renderer._ASYNC_STATE_RESTORED = False


def card_payload(**overrides):
    payload = {
        "mode": "designed_card",
        "input_urls": ["https://example.com/artwork.jpg"],
        "expected_input_count": 1,
        "module": "photo_guide",
        "template_family": "listing_specific_editorial_card_v1",
        "asset_roles": [{"role": "listing_artwork", "url": "https://example.com/artwork.jpg", "exact_pixel_preservation": True, "transform_allowed": False}],
        "listing_assets": [{"role": "listing_artwork"}],
        "card_brief": {"headline": "Photo Guide", "body": "Use a clear photo.", "bullets": ["Good daylight", "Full subject"]},
        "prohibited_elements": sorted(renderer.DESIGNED_CARD_PROHIBITIONS),
    }
    payload.update(overrides)
    return payload


def test_public_modes_are_codex_generation_only():
    assert renderer.APP_VERSION == "1.14.0"
    assert set(renderer.ALLOWED_MODES) == {"minimal_frame", "lifestyle", "orientation", "decorative_asset", "designed_card"}
    assert "deterministic_frame" not in renderer.ALLOWED_MODES
    assert "deterministic_lifestyle" not in renderer.ALLOWED_MODES
    assert "deterministic_card" not in renderer.ALLOWED_MODES
    assert "deterministic_raster_card" not in renderer.ALLOWED_MODES
    assert all(value["output_kind"] in {"final_asset", "decorative_asset"} for value in renderer.MODE_CONTRACTS.values())


def test_static_contract_has_no_local_customer_compositor():
    source = Path(renderer.__file__).read_text(encoding="utf-8").lower()
    for forbidden in ("deterministic_frame", "deterministic_lifestyle", "deterministic_card", "deterministic_raster_card", "pillow", "imagedraw", "image.new(", "alpha_composite"):
        assert forbidden not in source
    assert '"local_visual_compositing_allowed": false' in source
    assert "codex_generated_final_raster" in source


@pytest.mark.parametrize("mode", ["deterministic_frame", "deterministic_lifestyle", "deterministic_card", "deterministic_raster_card"])
def test_removed_modes_are_rejected(mode):
    with pytest.raises((ValueError, ValidationError), match="invalid_render_mode"):
        renderer.RenderRequest(mode=mode, input_urls=["https://example.com/a.jpg"])


def test_minimal_frame_and_lifestyle_prompts_require_complete_codex_raster():
    minimal = renderer._prompt("minimal_frame", "car portrait")
    lifestyle = renderer._prompt("lifestyle", "pet watercolor")
    for prompt in (minimal, lifestyle):
        assert "built-in image_gen/image_generation tool exactly once" in prompt
        assert "complete customer-facing Etsy raster" in prompt
        assert "no other process may draw" in prompt
        assert "complete subject visible" in prompt
    assert "physical neutral frame" in minimal
    assert "untouched room reference" in lifestyle


def test_designed_card_is_complete_codex_output_with_exact_copy():
    request = renderer.RenderRequest.model_validate(card_payload())
    prompt = renderer._prompt(request)
    assert "complete premium Etsy gallery card" in prompt
    assert "Photo Guide" in prompt
    assert "Use exactly the approved card_brief headline, body, and bullets" in prompt
    assert request.mode == "designed_card"


def test_lifestyle_accepts_scene_reference_and_artwork_as_inputs():
    request = renderer.RenderRequest(
        mode="lifestyle",
        input_urls=["https://example.com/room.jpg", "https://example.com/art.jpg"],
        asset_roles=[
            {"role": "scene_reference", "url": "https://example.com/room.jpg", "preservation": "reference_only"},
            {"role": "approved_listing_artwork", "url": "https://example.com/art.jpg", "exact_pixel_preservation": True, "transform_allowed": False},
        ],
        generation_instructions={"preserve_complete_source_subject": True, "scene_reference_is_not_final": True},
    )
    assert request.input_urls[-1].endswith("art.jpg")
    assert renderer._role_contract(request)["output_kind"] == "final_asset"


def test_codex_command_and_app_server_input_are_generation_paths(tmp_path):
    command = renderer._codex_app_server_command()
    assert command[:2] == ["codex", "app-server"]
    assert command[command.index("--enable") + 1] == "image_generation"
    inputs = renderer._codex_app_server_inputs("generate final raster", [tmp_path / "photo.jpg", tmp_path / "art.png"])
    assert inputs[0]["type"] == "text"
    assert [item["type"] for item in inputs[1:3]] == ["localImage", "localImage"]
    assert inputs[3]["type"] == "skill"


def test_fresh_capability_requires_all_current_codex_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER_DATA_DIR", str(tmp_path / "render-data"))
    assert renderer._fresh_capability_status()["fresh_gallery_capability_verified"] is False
    proof = {
        "schema_version": renderer.FRESH_PROOF_SCHEMA_VERSION,
        "image_pipeline_version": renderer.IMAGE_PIPELINE_VERSION,
        "modes": {mode: {"image_pipeline_version": renderer.IMAGE_PIPELINE_VERSION, "event_summary": "item.completed;items=image_generation_call"} for mode in renderer.REQUIRED_FRESH_MODES},
    }
    renderer._fresh_proof_path().write_text(json.dumps(proof), encoding="utf-8")
    status = renderer._fresh_capability_status()
    assert status["fresh_gallery_capability_verified"] is True
    assert status["verified_fresh_modes"] == sorted(renderer.REQUIRED_FRESH_MODES)


def test_codex_event_summary_requires_image_generation_event():
    raw = '{"type":"thread.started"}\n{"type":"item.completed","item":{"type":"imageGeneration"}}'
    summary = renderer._codex_event_summary(raw)
    assert "thread.started" in summary
    assert "image_generation_call" in summary


def test_readiness_is_not_satisfied_by_another_image_provider(monkeypatch):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    monkeypatch.setattr(renderer.shutil, "which", lambda name: None)
    monkeypatch.setenv("OPENAI_API_KEY", "configured-api-fallback")
    status = renderer.readiness()
    assert status["ready"] is False
    assert status["api_fallback_configured"] is True
    assert status["api_fallback_policy"] == "confirmed_codex_quota_only"
    assert status["customer_facing_generation"] == "codex_image_generation_only"
    assert status["local_visual_compositing_allowed"] is False


def test_render_endpoint_requires_auth(monkeypatch):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    response = TestClient(renderer.app).post("/render", json={"mode": "orientation", "input_urls": ["https://example.com/a.png"]})
    assert response.status_code == 401


def test_render_invokes_codex_for_minimal_frame_lifestyle_and_card(monkeypatch, tmp_path):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    monkeypatch.setattr(renderer, "_validate_public_https_url", lambda value: value)
    monkeypatch.setattr(renderer, "readiness", lambda: {"ready": True})
    calls = []

    def fake_download(url, target):
        path = target.with_suffix(".png")
        path.write_bytes(PNG)
        return path

    def fake_run(workspace, inputs, prompt, timeout):
        calls.append((len(inputs), prompt))
        output = workspace / "rendered-output.png"
        output.write_bytes(PNG + b"codex")
        event = json.dumps({"type": "item.completed", "item": {"type": "imageGeneration"}})
        return renderer._CodexRun(0, event, "", (output,))

    monkeypatch.setattr(renderer, "_download_image", fake_download)
    monkeypatch.setattr(renderer, "_run_codex_app_server", fake_run)
    client = TestClient(renderer.app)
    requests = [
        {"mode": "minimal_frame", "input_urls": ["https://example.com/art.jpg"]},
        {"mode": "lifestyle", "input_urls": ["https://example.com/room.jpg", "https://example.com/art.jpg"]},
        card_payload(),
    ]
    for payload in requests:
        response = client.post("/render", headers=AUTH, json=payload)
        assert response.status_code == 200
        assert response.headers["x-composition-mode"] == "codex_generated_final_raster"
    assert [count for count, _ in calls] == [1, 2, 1]



def test_confirmed_codex_quota_uses_reference_aware_api_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.setattr(renderer, "_validate_public_https_url", lambda value: value)
    monkeypatch.setattr(renderer, "readiness", lambda: {"ready": True})
    captured = {}

    def fake_download(url, target):
        path = target.with_suffix(".png")
        path.write_bytes(PNG + url.encode())
        return path

    def quota_run(workspace, inputs, prompt, timeout):
        return renderer._CodexRun(1, "", "insufficient_quota: usage limit reached", ())

    def fallback_generate(prompt, inputs, timeout):
        captured["prompt"] = prompt
        captured["inputs"] = [path.name for path in inputs]
        return PNG + b"api", "image/png", "gpt-image-2"

    monkeypatch.setattr(renderer, "_download_image", fake_download)
    monkeypatch.setattr(renderer, "_run_codex_app_server", quota_run)
    monkeypatch.setattr(renderer.openai_fallback, "generate_image", fallback_generate)
    monkeypatch.setattr(renderer.openai_fallback, "mark_codex_quota_exhausted", lambda: 1.0)
    response = TestClient(renderer.app).post(
        "/render", headers=AUTH,
        json={"mode": "lifestyle", "input_urls": ["https://example.com/room.jpg", "https://example.com/art.jpg"]},
    )
    assert response.status_code == 200
    assert response.headers["x-image-provider"] == "openai-api"
    assert response.headers["x-image-fallback"] == "true"
    assert response.headers["x-image-fallback-reason"] == "quota"
    assert len(captured["inputs"]) == 2
    assert "complete editorial lifestyle raster" in captured["prompt"]


@pytest.mark.parametrize("failure", [
    "codex_app_server_timeout",
    "network failure",
    "429 rate limited",
    "500 upstream capacity",
    "malformed provider result",
])
def test_nonquota_codex_failures_never_use_api_fallback(monkeypatch, tmp_path, failure):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.setattr(renderer, "_validate_public_https_url", lambda value: value)
    monkeypatch.setattr(renderer, "readiness", lambda: {"ready": True})
    calls = []

    def fake_download(url, target):
        path = target.with_suffix(".png")
        path.write_bytes(PNG)
        return path

    monkeypatch.setattr(renderer, "_download_image", fake_download)
    monkeypatch.setattr(renderer, "_run_codex_app_server", lambda *args: renderer._CodexRun(1, "", failure, ()))
    monkeypatch.setattr(renderer.openai_fallback, "generate_image", lambda *args: calls.append(args))
    response = TestClient(renderer.app).post(
        "/render", headers=AUTH,
        json={"mode": "minimal_frame", "input_urls": ["https://example.com/art.jpg"]},
    )
    assert response.status_code == 503
    assert calls == []


def test_codex_auth_failure_never_uses_api_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.setattr(renderer, "_validate_public_https_url", lambda value: value)
    monkeypatch.setattr(renderer, "readiness", lambda: {"ready": True})
    calls = []

    def fake_download(url, target):
        path = target.with_suffix(".png")
        path.write_bytes(PNG)
        return path

    monkeypatch.setattr(renderer, "_download_image", fake_download)
    monkeypatch.setattr(renderer, "_run_codex_app_server", lambda *args: renderer._CodexRun(1, "", "401 unauthorized", ()))
    monkeypatch.setattr(renderer.openai_fallback, "generate_image", lambda *args: calls.append(args))
    response = TestClient(renderer.app).post(
        "/render", headers=AUTH,
        json={"mode": "minimal_frame", "input_urls": ["https://example.com/art.jpg"]},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "codex_authentication_failed"
    assert calls == []


def test_async_success_persists_provider_metadata(monkeypatch, tmp_path):
    request = renderer.RenderRequest(mode="orientation", input_urls=["https://example.com/a.jpg"])
    job_id = "provider-meta-job"
    request_hash = renderer._request_hash(request)
    renderer._ASYNC_JOBS[job_id] = {
        "status": "queued", "created_at": 1.0, "request_hash": request_hash,
        "request": request.model_dump(mode="json"),
    }
    renderer._ASYNC_HASH_INDEX[request_hash] = job_id
    monkeypatch.setattr(renderer, "_render", lambda req: (
        PNG, "image/png", "digest", {
            "provider": "openai-api", "fallback_used": True,
            "fallback_reason": "quota", "model": "gpt-image-2",
        }
    ))
    renderer._run_async_job(job_id, request)
    job = renderer._load_async_job(job_id)
    assert job["provider"] == "openai-api"
    assert job["fallback_used"] is True
    assert job["fallback_reason"] == "quota"

def test_completed_matching_async_requests_are_reused(monkeypatch):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    monkeypatch.setattr(renderer, "_validate_public_https_url", lambda value: value)
    monkeypatch.setattr(renderer, "readiness", lambda: {"ready": True})
    monkeypatch.setattr(renderer.threading, "Thread", lambda **kwargs: type("NoStart", (), {"start": lambda self: None})())
    client = TestClient(renderer.app)
    request = {"mode": "orientation", "input_urls": ["https://example.com/same.jpg"]}
    first = client.post("/render-async", headers=AUTH, json=request)
    second = client.post("/render-async", headers=AUTH, json=request)
    assert first.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    job_id = first.json()["job_id"]
    renderer._ASYNC_JOBS[job_id].update({"status": "succeeded", "mime": "image/png", "output_sha256": "sha"})
    assert client.post("/render-async", headers=AUTH, json=request).json()["job_id"] == job_id


def test_failed_async_job_can_retry_without_duplicate_completion(monkeypatch):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    monkeypatch.setattr(renderer, "_validate_public_https_url", lambda value: value)
    monkeypatch.setattr(renderer, "readiness", lambda: {"ready": True})
    monkeypatch.setattr(renderer.threading, "Thread", lambda **kwargs: type("NoStart", (), {"start": lambda self: None})())
    monkeypatch.setattr(renderer, "_render", lambda request: (_ for _ in ()).throw(RuntimeError("codex_render_failed")))
    client = TestClient(renderer.app)
    request = renderer.RenderRequest(mode="orientation", input_urls=["https://example.com/a.jpg"])
    first = client.post("/render-async", headers=AUTH, json=request.model_dump())
    old_id = first.json()["job_id"]
    renderer._run_async_job(old_id, request)
    retry = client.post("/render-async", headers=AUTH, json=request.model_dump())
    assert retry.status_code == 202
    assert retry.json()["job_id"] != old_id


def test_container_and_dependencies_do_not_restore_local_compositor():
    root = Path(renderer.__file__).parents[0]
    compose = (root / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    entrypoint = (root / "runtime-entrypoint.sh").read_text(encoding="utf-8")
    combined = "\n".join((compose, dockerfile, requirements, entrypoint)).lower()
    assert "pillow" not in combined
    assert "openai_image_fallback" not in combined
    assert "continuing so" not in entrypoint


def test_duplicate_request_hash_is_fail_closed():
    request = renderer.RenderRequest(mode="orientation", input_urls=["https://example.com/same.jpg"])
    digest = renderer._request_hash(request)
    renderer._claim_request(digest)
    with pytest.raises(ValueError, match="duplicate_request_hash"):
        renderer._claim_request(digest)

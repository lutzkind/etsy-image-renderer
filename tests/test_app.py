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


def designed_payload(**overrides):
    payload = {
        "mode": "designed_card",
        "input_urls": ["https://example.com/listing.jpg", "https://example.com/detail.jpg"],
        "expected_input_count": 2,
        "module": "listing_gallery",
        "template_family": "editorial_card_v1",
        "asset_roles": [
            {"role": "listing_photo", "url": "https://example.com/listing.jpg", "preservation": "subject_identity"},
            {"role": "detail_photo", "url": "https://example.com/detail.jpg", "preservation": "exact_artwork"},
        ],
        "listing_assets": [{"role": "listing_photo"}, {"role": "detail_photo"}],
        "layout_contract": {"aspect_ratio": "4:5", "headline_position": "top"},
        "card_brief": {"headline": "Handmade for Every Day", "body": "Thoughtful details for your space.", "bullets": ["Solid wood", "Made to order"]},
        "generation_instructions": {"palette": "warm natural", "finish": "editorial"},
        "prohibited_elements": sorted(renderer.DESIGNED_CARD_PROHIBITIONS),
    }
    payload.update(overrides)
    return payload


def test_private_urls_are_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="input_url_not_public"):
        renderer._validate_public_https_url("https://example.test/a.png")


def test_mode_contracts_keep_legacy_counts_and_support_structured_modes():
    assert renderer.APP_VERSION == "1.4.3"
    assert renderer.EXPECTED_INPUTS == {
        "minimal_frame": 1, "lifestyle": 2, "orientation": 1,
        "before_after_card": 2, "information_card": 2,
    }
    request = renderer.RenderRequest(
        mode="decorative_asset", expected_input_count=2,
        asset_roles=[
            {"role": "source_photo", "url": "https://example.com/source.jpg", "preservation": "subject_identity"},
            {"role": "style_anchor", "url": "https://example.com/art.jpg", "preservation": "style_only"},
        ], prohibited_elements=renderer.STRICT_NO_TEXT, module="photo_guide",
        template_family="correct_wrong_photo_guide_v1",
    )
    assert request.input_urls == ["https://example.com/source.jpg", "https://example.com/art.jpg"]
    assert renderer._role_contract(request)["contract_version"] == renderer.CONTRACT_VERSION


def test_decorative_asset_remains_strict_no_text():
    request = renderer.RenderRequest(
        mode="decorative_asset", expected_input_count=1,
        asset_roles=[{"role": "source", "url": "https://example.com/a.png"}],
        prohibited_elements=renderer.STRICT_NO_TEXT,
    )
    contract = renderer._role_contract(request)
    assert set(renderer.STRICT_NO_TEXT).issubset(set(contract["prohibited_elements"]))
    assert "Never generate words" in renderer._prompt(request)


def test_decorative_asset_rejects_missing_required_contract_parts():
    with pytest.raises(ValueError, match="strict_no_text"):
        renderer.RenderRequest(mode="decorative_asset", expected_input_count=1, asset_roles=[{"role": "source", "url": "https://example.com/a.png"}], prohibited_elements=["text"])
    with pytest.raises(ValueError, match="asset_roles"):
        renderer.RenderRequest(mode="decorative_asset", expected_input_count=1, input_urls=["https://example.com/a.png"], prohibited_elements=renderer.STRICT_NO_TEXT)
    with pytest.raises(ValueError, match="exact_input_count"):
        renderer.RenderRequest(mode="decorative_asset", expected_input_count=0, asset_roles=[{"role": "source", "url": "https://example.com/a.png"}], prohibited_elements=renderer.STRICT_NO_TEXT)


@pytest.mark.parametrize(
    ("field", "expected", "match"),
    [
        ("asset_roles", [], "designed_card_requires_asset_roles"),
        ("listing_assets", [], "designed_card_requires_listing_assets"),
        ("expected_input_count", 1, "designed_card_expected_input_count_mismatch"),
        ("input_urls", ["https://example.com/listing.jpg"], "designed_card_input_count_mismatch"),
        ("module", "", "designed_card_requires_module"),
        ("template_family", "", "designed_card_requires_template_family"),
        ("card_brief", {}, "designed_card_requires_headline"),
        ("prohibited_elements", [], "designed_card_requires_prohibitions"),
    ],
)
def test_designed_card_each_required_validation_failure(field, expected, match):
    payload = designed_payload(**{field: expected})
    with pytest.raises((ValueError, ValidationError), match=match):
        renderer.RenderRequest.model_validate(payload)


def test_designed_card_valid_contract():
    request = renderer.RenderRequest.model_validate(designed_payload())
    assert request.mode == "designed_card"
    assert request.expected_input_count == 2
    assert len(request.listing_assets) == 2
    assert renderer._role_contract(request)["generated_text"] is True


def test_designed_card_prompt_contains_exact_contract_and_rejects_generic_styling():
    request = renderer.RenderRequest.model_validate(designed_payload(template_reference_url="https://example.com/template.png"))
    prompt = renderer._prompt(request)
    for text in ["Handmade for Every Day", "Thoughtful details for your space.", "Solid wood", "Made to order", "STRUCTURED INPUT CONTRACT", "layout_contract", "listing_assets", "GENERATION INSTRUCTIONS", "Use the built-in image_gen/image_generation tool exactly once", "inspiration only", "never copy it exactly"]:
        assert text in prompt
    for text in ["dashboard", "presentation slides", "ivory-panel", "Canva-like"]:
        assert text.lower() in prompt.lower()
    assert "generic information panels" in prompt


def test_prompt_preserves_exact_pixel_and_forbids_direct_api_fallback():
    request = renderer.RenderRequest(
        mode="decorative_asset", expected_input_count=2,
        asset_roles=[
            {"role": "source_photo", "url": "https://example.com/source.jpg", "preservation": "subject_identity"},
            {"role": "listing_artwork", "url": "https://example.com/art.jpg", "exact_pixel_preservation": True, "transform_allowed": False},
        ], prohibited_elements=renderer.STRICT_NO_TEXT, module="photo_guide",
        template_family="correct_wrong_photo_guide_v1",
    )
    prompt = renderer._prompt(request)
    assert "built-in image_gen/image_generation tool exactly once" in prompt
    assert "Do not call an external image API" in prompt
    assert "EXACT PIXEL PRESERVATION REQUIRED" in prompt
    assert "blank caption sheets" in prompt
    assert "generic information panel" in prompt


def test_template_reference_is_extra_command_image_without_changing_listing_count(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer, "_validate_public_https_url", lambda value: value)
    monkeypatch.setattr(renderer, "readiness", lambda: {"ready": True})
    monkeypatch.setenv("RENDER_DATA_DIR", str(tmp_path))
    downloaded = []
    command_seen = {}

    def fake_download(url, target):
        downloaded.append((url, target.name))
        path = target.with_suffix(".png")
        path.write_bytes(PNG)
        return path

    def fake_run(command, **kwargs):
        command_seen["command"] = command
        command_seen["prompt"] = kwargs["input"]
        workspace = Path(command[command.index("-C") + 1])
        output = workspace / "rendered-output.png"
        output.write_bytes(PNG + b"out")
        event = json.dumps({"type": "item.completed", "item": {"type": "image_generation_call"}})
        return subprocess.CompletedProcess(command, 0, stdout=event, stderr="")

    monkeypatch.setattr(renderer, "_download_image", fake_download)
    monkeypatch.setattr(renderer.subprocess, "run", fake_run)
    request = renderer.RenderRequest.model_validate(designed_payload(template_reference_url="https://example.com/template.png"))
    data, mime, _ = renderer._render(request)
    assert data == PNG + b"out"
    assert mime == "image/png"
    assert len(request.input_urls) == 2
    assert len(downloaded) == 3
    assert command_seen["command"].count("-i") == 3
    assert "DESIGN REFERENCE ONLY" in command_seen["prompt"]
    assert "inspiration-only" in command_seen["prompt"]
    proof = renderer._load_fresh_proof()
    assert proof["app_version"] == renderer.APP_VERSION
    assert proof["modes"]["designed_card"]["output_sha256"]


def test_legacy_lifestyle_prompt_keeps_image_two_ground_truth():
    prompt = renderer._prompt("lifestyle", "pet portrait")
    assert "second asset as the exact artwork target" in prompt
    assert "preserve the room" in prompt


def test_command_enables_image_generation(tmp_path):
    command = renderer._codex_command(tmp_path, [tmp_path / "a.png"])
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("--enable") + 1] == "image_generation"
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "-c" in command
    assert 'approval_policy="never"' in command
    assert "--ignore-user-config" in command
    assert command[-1] == "-"


def test_codex_event_summary_distinguishes_image_tool_events():
    raw = '\n'.join([
        '{"type":"thread.started"}',
        '{"type":"item.completed","item":{"type":"image_generation_call"}}',
    ])
    summary = renderer._codex_event_summary(raw)
    assert "image_generation_call" in summary
    assert "thread.started" in summary
    assert renderer._stderr_markers("image tool unavailable") == "image,tool"


def test_fresh_capability_status_requires_current_generation_proofs(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER_DATA_DIR", str(tmp_path / "render-data"))
    initial = renderer._fresh_capability_status()
    assert initial["fresh_render_verified"] is False
    assert initial["fresh_gallery_capability_verified"] is False

    proof = {
        "schema_version": renderer.FRESH_PROOF_SCHEMA_VERSION,
        "app_version": renderer.APP_VERSION,
        "modes": {
            mode: {"app_version": renderer.APP_VERSION, "event_summary": "item.completed;items=image_generation_call"}
            for mode in renderer.REQUIRED_FRESH_MODES
        },
    }
    renderer._fresh_proof_path().write_text(json.dumps(proof), encoding="utf-8")
    verified = renderer._fresh_capability_status()
    assert verified["fresh_render_verified"] is True
    assert verified["fresh_gallery_capability_verified"] is True
    assert verified["verified_fresh_modes"] == sorted(renderer.REQUIRED_FRESH_MODES)


def test_container_runtime_is_sandboxed_and_auth_mount_is_read_only():
    compose = Path(__file__).parents[1] / "docker-compose.yaml"
    dockerfile = Path(__file__).parents[1] / "Dockerfile"
    entrypoint = Path(__file__).parents[1] / "runtime-entrypoint.sh"
    compose_text = compose.read_text(encoding="utf-8")
    dockerfile_text = dockerfile.read_text(encoding="utf-8")
    entrypoint_text = entrypoint.read_text(encoding="utf-8")

    assert "read_only: true" in compose_text
    assert "cap_drop: [ALL]" in compose_text
    assert "no-new-privileges:true" in compose_text
    assert "/run/secrets/codex-auth.json:ro" in compose_text
    assert ":/root/.codex/auth.json" not in compose_text
    assert "CODEX_HOME=/tmp/etsy-codex-home" in compose_text
    assert "setpriv" in entrypoint_text
    assert "auth_source=/root/.codex/auth.json" in entrypoint_text
    assert 'chown -R "$runtime_uid:$runtime_gid" "$render_data_dir"' in entrypoint_text
    assert 'chown "$runtime_uid:0" "$codex_home"' in entrypoint_text
    assert 'chmod 0770 "$codex_home"' in entrypoint_text
    assert "codex-system-skills/imagegen" in entrypoint_text
    assert "codex-system-skills/imagegen" in dockerfile_text
    assert "chown 10001:0 /tmp/etsy-codex-home" in dockerfile_text
    assert "chmod 0770 /tmp/etsy-codex-home" in dockerfile_text


def test_render_endpoint_requires_auth(monkeypatch):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    response = TestClient(renderer.app).post("/render", json={"mode": "orientation", "input_urls": ["https://example.com/a.png"]})
    assert response.status_code == 401


def test_sync_render_is_backward_compatible_and_duplicate_safe(monkeypatch):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    request = {"mode": "orientation", "input_urls": ["https://example.com/a.png"]}

    def fake_render(req):
        renderer._claim_request(renderer._request_hash(req))
        return PNG, "image/png", "digest"

    monkeypatch.setattr(renderer, "_render", fake_render)
    client = TestClient(renderer.app)
    first = client.post("/render", headers=AUTH, json=request)
    second = client.post("/render", headers=AUTH, json=request)
    assert first.status_code == 200
    assert first.content == PNG
    assert first.headers["x-renderer-version"] == renderer.APP_VERSION
    assert second.status_code == 409
    assert second.json()["detail"] == "duplicate_request_hash"


def test_async_repeated_request_reuses_job_while_queued_running_and_succeeded(monkeypatch):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    monkeypatch.setattr(renderer, "_validate_public_https_url", lambda value: value)
    monkeypatch.setattr(renderer, "readiness", lambda: {"ready": True})
    monkeypatch.setattr(renderer.threading, "Thread", lambda **kwargs: type("NoStart", (), {"start": lambda self: None})())
    client = TestClient(renderer.app)
    request = {"mode": "orientation", "input_urls": ["https://example.com/a.png"]}
    first = client.post("/render-async", headers=AUTH, json=request)
    job_id = first.json()["job_id"]
    assert first.status_code == 202
    assert client.post("/render-async", headers=AUTH, json=request).json()["job_id"] == job_id
    renderer._ASYNC_JOBS[job_id]["status"] = "running"
    assert client.post("/render-async", headers=AUTH, json=request).json()["job_id"] == job_id
    renderer._ASYNC_JOBS[job_id].update({"status": "succeeded", "mime": "image/png", "output_sha256": "abc"})
    completed = client.post("/render-async", headers=AUTH, json=request)
    assert completed.json()["job_id"] == job_id
    assert completed.json()["result_url"].endswith("/result")


def test_failed_async_job_releases_request_index_for_retry(monkeypatch):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    monkeypatch.setattr(renderer, "_validate_public_https_url", lambda value: value)
    monkeypatch.setattr(renderer, "readiness", lambda: {"ready": True})
    monkeypatch.setattr(renderer.threading, "Thread", lambda **kwargs: type("NoStart", (), {"start": lambda self: None})())
    monkeypatch.setattr(renderer, "_render", lambda request: (_ for _ in ()).throw(RuntimeError("failed")))
    client = TestClient(renderer.app)
    request = renderer.RenderRequest(mode="orientation", input_urls=["https://example.com/a.png"])
    first = client.post("/render-async", headers=AUTH, json=request.model_dump())
    old_id = first.json()["job_id"]
    renderer._run_async_job(old_id, request)
    assert renderer._ASYNC_JOBS[old_id]["status"] == "failed"
    retry = client.post("/render-async", headers=AUTH, json=request.model_dump())
    assert retry.status_code == 202
    assert retry.json()["job_id"] != old_id


def test_completed_async_status_is_json_with_metadata_and_result_url(monkeypatch):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    job_id = "completed"
    renderer._ASYNC_JOBS[job_id] = {"status": "succeeded", "request_hash": "req", "mime": "image/png", "output_sha256": "sha", "data": PNG}
    response = TestClient(renderer.app).get(f"/render-async/{job_id}", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["result_url"] == f"/render-async/{job_id}/result"
    assert body["mime"] == "image/png"
    assert body["output_sha256"] == "sha"
    assert body["app_version"] == renderer.APP_VERSION


def test_async_result_binary_and_non_success_states(monkeypatch):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    renderer._ASYNC_JOBS["queued"] = {"status": "queued"}
    renderer._ASYNC_JOBS["failed"] = {"status": "failed", "error": "codex_render_failed"}
    renderer._ASYNC_JOBS["done"] = {"status": "succeeded", "data": PNG, "mime": "image/png", "output_sha256": "sha", "request_hash": "req"}
    client = TestClient(renderer.app)
    assert client.get("/render-async/queued/result", headers=AUTH).status_code == 202
    assert client.get("/render-async/failed/result", headers=AUTH).status_code == 409
    result = client.get("/render-async/done/result", headers=AUTH)
    assert result.status_code == 200
    assert result.content == PNG
    assert result.headers["x-image-sha256"] == "sha"
    assert client.get("/render-async/missing/result", headers=AUTH).status_code == 404


def test_duplicate_request_hash_is_fail_closed():
    request = renderer.RenderRequest(mode="orientation", input_urls=["https://example.com/same.jpg"])
    digest = renderer._request_hash(request)
    renderer._claim_request(digest)
    with pytest.raises(ValueError, match="duplicate_request_hash"):
        renderer._claim_request(digest)

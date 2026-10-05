"""Deterministic tests for rolling current GPT Image model selection.

These tests lock in the production end state:

* the primary Codex path never pins a numbered GPT Image model and never
  fabricates one in provenance;
* the paid OpenAI API fallback automatically resolves the newest stable
  full-capability GPT Image model from the account catalog, with bounded
  cache/last-known/baseline failure behavior;
* no static exact-name capability allowlist blocks the next normal GPT Image
  release;
* the explicit paid-authorization spend gate is unchanged.

No network or billable call is made: the provider/catalog boundary is mocked.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as renderer
import openai_fallback

PNG = b"\x89PNG\r\n\x1a\nmodel-selection-output"
AUTH = {"Authorization": "Bearer secret"}

# Fixture verified against the live OpenAI account on 2026-10-05.
LIVE_CATALOG = [
    {"id": "chatgpt-image-latest", "created": 1765925279, "owned_by": "system"},
    {"id": "gpt-image-1", "created": 1745517030, "owned_by": "system"},
    {"id": "gpt-image-1-mini", "created": 1758845821, "owned_by": "system"},
    {"id": "gpt-image-1.5", "created": 1764030620, "owned_by": "system"},
    {"id": "gpt-image-2", "created": 1776399795, "owned_by": "system"},
    {"id": "gpt-image-2-2026-04-21", "created": 1776399994, "owned_by": "system"},
    {"id": "gpt-image-2.5-flare", "created": 1788563147, "owned_by": "system"},
    {"id": "gpt-image-2.5-flare-2026-09-08", "created": 1788851006, "owned_by": "system"},
    {"id": "gpt-image-2.5-sunburst", "created": 1788563162, "owned_by": "system"},
    {"id": "gpt-image-2.5-sunburst-2026-09-08", "created": 1788851010, "owned_by": "system"},
]


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER_DATA_DIR", str(tmp_path / "render-data"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.delenv("OPENAI_IMAGE_FALLBACK_IMAGE_MODEL", raising=False)
    renderer._REQUEST_DIGESTS.clear()
    renderer._ASYNC_JOBS.clear()
    renderer._ASYNC_HASH_INDEX.clear()
    renderer._ASYNC_QUEUE_IDS.clear()
    renderer._ASYNC_STATE_RESTORED = False
    openai_fallback.reset_quota_circuit()
    openai_fallback.reset_image_model_state()
    renderer.codex_quota.reset_cache()
    yield
    renderer._REQUEST_DIGESTS.clear()
    renderer._ASYNC_JOBS.clear()
    renderer._ASYNC_HASH_INDEX.clear()
    renderer._ASYNC_QUEUE_IDS.clear()
    renderer._ASYNC_STATE_RESTORED = False
    openai_fallback.reset_quota_circuit()
    openai_fallback.reset_image_model_state()
    renderer.codex_quota.reset_cache()


def _fake_download(url, target):
    path = target.with_suffix(".png")
    path.write_bytes(PNG + str(url).encode())
    return path


def _fake_codex_run(extra_item: dict | None = None):
    def fake_run(workspace, inputs, prompt, timeout):
        output = workspace / "rendered-output.png"
        output.write_bytes(PNG)
        item = {"type": "imageGeneration"}
        if extra_item:
            item.update(extra_item)
        raw = json.dumps({"type": "item.completed", "item": item})
        return renderer._CodexRun(0, raw, "", (output,))
    return fake_run


def _render_client(monkeypatch, extra_item: dict | None = None) -> TestClient:
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    monkeypatch.setattr(renderer, "_validate_public_https_url", lambda value: value)
    monkeypatch.setattr(renderer, "readiness", lambda: {"ready": True})
    monkeypatch.setattr(renderer, "_download_image", _fake_download)
    monkeypatch.setattr(renderer, "_run_codex_app_server", _fake_codex_run(extra_item))
    return TestClient(renderer.app)


# ---------------------------------------------------------------------------
# 1. Primary Codex path is unversioned
# ---------------------------------------------------------------------------


def test_primary_codex_invocation_does_not_pin_a_numbered_gpt_image_model():
    command = renderer._codex_app_server_command()
    assert command == ["codex", "app-server", "--enable", "image_generation", "--listen", "stdio://"]
    assert not any("gpt-image" in token for token in command)


# ---------------------------------------------------------------------------
# 2-4. Truthful primary-path provenance
# ---------------------------------------------------------------------------


def test_primary_render_provenance_is_provider_managed_not_fabricated(monkeypatch):
    client = _render_client(monkeypatch)
    response = client.post(
        "/render", headers=AUTH,
        json={"mode": "minimal_frame", "input_urls": ["https://example.com/art.jpg"]},
    )
    assert response.status_code == 200, response.text
    headers = dict(response.headers)
    assert headers["x-image-provider"] == "codex-image"
    assert headers["x-image-model"] == "provider-selected"
    assert headers["x-image-model-selection-policy"] == "codex-provider-default"
    assert headers["x-image-model-observed"] == "false"
    assert headers["x-image-model-configured-override"] == ""
    assert "gpt-image-2" not in json.dumps(headers)


def test_actual_model_is_captured_when_codex_event_exposes_it(monkeypatch):
    event = json.dumps({
        "type": "item.completed",
        "item": {"type": "imageGeneration", "model": "gpt-image-9-aurora"},
    })
    assert renderer._codex_event_image_model(event) == "gpt-image-9-aurora"
    # A turn/thread model is the mainline text model, never image evidence.
    assert renderer._codex_event_image_model(
        json.dumps({"type": "turn.completed", "params": {"turn": {"model": "gpt-6-astra"}}})
    ) == ""

    client = _render_client(monkeypatch, {"model": "gpt-image-9-aurora"})
    response = client.post(
        "/render", headers=AUTH,
        json={"mode": "minimal_frame", "input_urls": ["https://example.com/art.jpg"]},
    )
    assert response.status_code == 200, response.text
    assert response.headers["x-image-model"] == "gpt-image-9-aurora"
    assert response.headers["x-image-model-observed"] == "true"


def test_provider_managed_label_is_used_when_codex_exposes_no_model():
    meta = renderer._codex_provider_meta("")
    assert meta["provider"] == "codex-image"
    assert meta["model"] == "provider-selected"
    assert meta["model_observed"] is False
    assert meta["model_selection_policy"] == "codex-provider-default"
    assert meta["model_configured_override"] == ""


# ---------------------------------------------------------------------------
# 5-12. Automatic API model resolution
# ---------------------------------------------------------------------------


def test_live_catalog_fixture_resolves_to_current_best_stable_model():
    assert openai_fallback.select_best_image_model(LIVE_CATALOG) == "gpt-image-2.5-sunburst"


def test_image_2_plus_2_5_variants_resolves_to_sunburst():
    fixture = [m for m in LIVE_CATALOG if m["id"] in {"gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"}]
    assert openai_fallback.select_best_image_model(fixture) == "gpt-image-2.5-sunburst"


def test_dated_snapshots_do_not_outrank_their_stable_alias():
    selected = openai_fallback.select_best_image_model(LIVE_CATALOG)
    assert selected == "gpt-image-2.5-sunburst"
    assert "2026-09-08" not in selected
    # When only a dated snapshot exists it remains eligible.
    assert openai_fallback.select_best_image_model(
        [{"id": "gpt-image-2.5-sunburst-2026-09-08", "created": 1788851010}]
    ) == "gpt-image-2.5-sunburst-2026-09-08"


def test_deprecated_chatgpt_image_latest_is_never_selected():
    fixture = [{"id": "chatgpt-image-latest", "created": 1765925279}, {"id": "gpt-image-1", "created": 1745517030}]
    assert openai_fallback.select_best_image_model(fixture) == "gpt-image-1"
    assert openai_fallback.select_best_image_model([{"id": "chatgpt-image-latest", "created": 1765925279}]) == ""


def test_mini_and_preview_models_never_become_the_premium_default():
    fixture = [
        {"id": "gpt-image-3-mini", "created": 1900000000},
        {"id": "gpt-image-3-preview", "created": 1899999999},
        {"id": "gpt-image-2.5-flare", "created": 1788563147},
    ]
    assert openai_fallback.select_best_image_model(fixture) == "gpt-image-2.5-flare"
    assert openai_fallback.select_best_image_model(
        [{"id": "gpt-image-3-mini", "created": 1900000000}]
    ) == ""


def test_synthetic_future_gpt_image_3_outranks_2_5_without_a_mapping_entry():
    fixture = LIVE_CATALOG + [{"id": "gpt-image-3", "created": 1900000000}]
    assert openai_fallback.select_best_image_model(fixture) == "gpt-image-3"
    fixture_variant = LIVE_CATALOG + [{"id": "gpt-image-3-nebula", "created": 1900000000}]
    assert openai_fallback.select_best_image_model(fixture_variant) == "gpt-image-3-nebula"


def test_numeric_version_ordering_handles_2_10_above_2_9():
    fixture = [{"id": "gpt-image-2.9", "created": 1}, {"id": "gpt-image-2.10", "created": 2}]
    assert openai_fallback.select_best_image_model(fixture) == "gpt-image-2.10"


def test_same_version_variant_selection_is_deterministic():
    fixture = [
        {"id": "gpt-image-3-flare", "created": 30},
        {"id": "gpt-image-3-sunburst", "created": 20},
    ]
    assert openai_fallback.select_best_image_model(fixture) == "gpt-image-3-sunburst"
    # Unknown same-tier variants fall back to created, then id, deterministically.
    unknown = [
        {"id": "gpt-image-3-zenith", "created": 200},
        {"id": "gpt-image-3-aurora", "created": 200},
    ]
    assert openai_fallback.select_best_image_model(unknown) == "gpt-image-3-zenith"
    assert openai_fallback.select_best_image_model(list(reversed(unknown))) == "gpt-image-3-zenith"


# ---------------------------------------------------------------------------
# 13-14. Explicit override and safe discovery failure
# ---------------------------------------------------------------------------


def test_explicit_operator_pin_overrides_automatic_selection(monkeypatch):
    monkeypatch.setenv("OPENAI_IMAGE_FALLBACK_IMAGE_MODEL", "gpt-image-1")
    called = []
    monkeypatch.setattr(openai_fallback, "_fetch_image_model_catalog", lambda: called.append(True) or LIVE_CATALOG)
    resolution = openai_fallback.resolve_image_model(allow_network=True)
    assert resolution.model == "gpt-image-1"
    assert resolution.policy == "explicit"
    assert resolution.source == "explicit"
    assert resolution.configured_override == "gpt-image-1"
    assert called == []

    # An explicit ``auto`` value means automatic policy, not a pin named auto.
    monkeypatch.setenv("OPENAI_IMAGE_FALLBACK_IMAGE_MODEL", "auto")
    monkeypatch.setattr(openai_fallback, "_fetch_image_model_catalog", lambda: LIVE_CATALOG)
    openai_fallback.reset_image_model_state()
    resolution = openai_fallback.resolve_image_model(allow_network=True)
    assert resolution.model == "gpt-image-2.5-sunburst"
    assert resolution.policy == "auto"
    assert resolution.source == "catalog"


def test_discovery_outage_uses_last_known_then_baseline(monkeypatch):
    monkeypatch.setattr(openai_fallback, "_fetch_image_model_catalog", lambda: LIVE_CATALOG)
    first = openai_fallback.resolve_image_model(allow_network=True)
    assert first.model == "gpt-image-2.5-sunburst"
    assert first.source == "catalog"

    def outage():
        raise openai_fallback.OpenAIImageFallbackError("openai_image_model_catalog_transport_failed")

    monkeypatch.setattr(openai_fallback, "_fetch_image_model_catalog", outage)
    openai_fallback._MODEL_STATE["fetched_at"] = 0.0  # force a catalog attempt
    cached = openai_fallback.resolve_image_model(allow_network=True)
    assert cached.model == "gpt-image-2.5-sunburst"
    assert cached.source == "last_known"
    assert "transport_failed" in cached.catalog_error

    openai_fallback.reset_image_model_state()
    baseline = openai_fallback.resolve_image_model(allow_network=True)
    assert baseline.model == openai_fallback._BASELINE_IMAGE_MODEL == "gpt-image-2.5-sunburst"
    assert baseline.source == "baseline"
    assert baseline.catalog_error


def test_resolution_cache_avoids_repeat_catalog_queries(monkeypatch):
    calls = []

    def catalog():
        calls.append(True)
        return LIVE_CATALOG

    monkeypatch.setattr(openai_fallback, "_fetch_image_model_catalog", catalog)
    assert openai_fallback.resolve_image_model(allow_network=True).source == "catalog"
    assert openai_fallback.resolve_image_model(allow_network=True).source == "cache"
    assert openai_fallback.resolve_image_model(allow_network=True).source == "cache"
    assert len(calls) == 1


def test_health_reports_policy_without_network_or_codex_probe(monkeypatch):
    monkeypatch.setenv("ETSY_CODEX_RENDERER_TOKEN", "secret")
    monkeypatch.setattr(
        openai_fallback, "_fetch_image_model_catalog",
        lambda: (_ for _ in ()).throw(AssertionError("health must not query the model catalog")),
    )
    body = TestClient(renderer.app).get("/health").json()
    assert body["primary_image_provider"] == "codex-image"
    assert body["primary_image_model"] == "provider-selected"
    assert body["primary_image_model_selection_policy"] == "codex-provider-default"
    assert body["primary_image_model_pinned"] is False
    assert body["api_fallback_image_model_policy"] == "auto"
    assert body["api_fallback_image_model_source"] == "baseline"
    assert body["api_fallback_image_model"] == "gpt-image-2.5-sunburst"
    assert body["api_fallback_authorized"] is False


# ---------------------------------------------------------------------------
# 15-18. Forward-compatible request construction and spend gate
# ---------------------------------------------------------------------------


def test_unseen_future_model_does_not_fail_capability_lookup():
    capabilities = openai_fallback.image_model_capabilities("gpt-image-4")
    assert capabilities["action"] is True
    assert capabilities["quality"] is True
    assert capabilities["size"] is True
    assert capabilities["output_format"] is True
    assert capabilities["input_fidelity"] is False
    legacy = openai_fallback.image_model_capabilities("gpt-image-1.5")
    assert legacy["input_fidelity"] is True
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match="unsupported_image_model"):
        openai_fallback.image_model_capabilities("some-future-non-gpt-image-model")


def test_unseen_future_model_builds_a_safe_request(monkeypatch, tmp_path):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            import base64
            return {"output": [{
                "type": "image_generation_call",
                "result": base64.b64encode(PNG).decode("ascii"),
            }]}

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
    monkeypatch.setattr(openai_fallback, "_fetch_image_model_catalog", lambda: [
        {"id": "gpt-image-4", "created": 1950000000},
    ])
    monkeypatch.setattr(openai_fallback.httpx, "Client", lambda **kwargs: FakeClient())
    source = tmp_path / "future-model-input.png"
    source.write_bytes(PNG + b"input")

    data, mime, model = openai_fallback.generate_image("future edit", [source], 120)

    tool = captured["payload"]["tools"][0]
    assert model == "gpt-image-4"
    assert tool["model"] == "gpt-image-4"
    assert tool["action"] == "edit"
    assert "input_fidelity" not in tool
    assert data.startswith(b"\x89PNG")


def test_current_2_5_request_omits_legacy_only_input_fidelity(monkeypatch):
    monkeypatch.setenv("OPENAI_IMAGE_FALLBACK_IMAGE_MODEL", "gpt-image-2.5-sunburst")
    tool = openai_fallback._tool_config(True, openai_fallback.image_model())
    assert tool["model"] == "gpt-image-2.5-sunburst"
    assert "input_fidelity" not in tool
    legacy_tool = openai_fallback._tool_config(True, "gpt-image-1")
    assert legacy_tool["input_fidelity"] == "high"


def test_paid_fallback_is_impossible_without_explicit_authorization(monkeypatch):
    sent = []
    monkeypatch.setenv("OPENAI_API_KEY", "api-key")
    monkeypatch.delenv("ALLOW_PAID_OPENAI_IMAGE_FALLBACK", raising=False)
    monkeypatch.setattr(openai_fallback, "_fetch_image_model_catalog", lambda: LIVE_CATALOG)
    monkeypatch.setattr(openai_fallback.httpx, "Client", lambda **kwargs: sent.append(kwargs))
    with pytest.raises(openai_fallback.OpenAIImageFallbackError, match="not_authorized"):
        openai_fallback.generate_image("make a frame", [], 120)
    assert sent == []
    assert openai_fallback.paid_fallback_authorized() is False


# ---------------------------------------------------------------------------
# 19. Sync and async surfaces agree; legacy jobs are normalized
# ---------------------------------------------------------------------------


def test_sync_and_async_provenance_metadata_agree(monkeypatch):
    client = _render_client(monkeypatch)
    sync = client.post(
        "/render", headers=AUTH,
        json={"mode": "minimal_frame", "input_urls": ["https://example.com/sync.jpg"]},
    )
    assert sync.status_code == 200, sync.text

    request = renderer.RenderRequest(mode="minimal_frame", input_urls=["https://example.com/async.jpg"])
    job_id = "model-agreement-job"
    request_hash = renderer._request_hash(request)
    renderer._ASYNC_JOBS[job_id] = {
        "status": "queued", "created_at": time.time(), "request_hash": request_hash,
        "request": request.model_dump(mode="json"),
    }
    renderer._ASYNC_HASH_INDEX[request_hash] = job_id
    renderer._run_async_job(job_id, request)

    status_payload = client.get(f"/render-async/{job_id}", headers=AUTH).json()
    result = client.get(f"/render-async/{job_id}/result", headers=AUTH)
    assert status_payload["status"] == "succeeded"
    for key in ("provider", "model", "model_selection_policy", "model_observed"):
        header_value = str(sync.headers[f"x-image-{key.replace('_', '-')}"])
        assert str(status_payload[key]).lower() == header_value.lower()
    assert status_payload["model"] == "provider-selected"
    assert result.headers["x-image-model"] == sync.headers["x-image-model"]
    assert result.headers["x-image-model-selection-policy"] == "codex-provider-default"
    assert "gpt-image-2" not in json.dumps(dict(result.headers))


def test_legacy_async_jobs_are_normalized_to_truthful_provenance(monkeypatch):
    legacy_id = "legacy-gpt-image-2-job"
    meta_path = renderer._async_job_meta_path(legacy_id)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({
        "status": "succeeded",
        "created_at": 1.0,
        "request_hash": "legacy",
        "provider": "codex-image",
        "fallback_used": False,
        "fallback_reason": "",
        "model": "gpt-image-2",
        "output_sha256": "deadbeef",
        "mime": "image/png",
    }), encoding="utf-8")
    renderer._restore_async_state()
    job = renderer._load_async_job(legacy_id)
    assert job["model"] == "provider-selected"
    assert job["model_observed"] is False
    assert job["model_selection_policy"] == "codex-provider-default"
    assert "gpt-image-2" not in json.dumps(job)


def test_fallback_provenance_reports_resolution_policy(monkeypatch):
    monkeypatch.delenv("OPENAI_IMAGE_FALLBACK_IMAGE_MODEL", raising=False)
    auto_meta = renderer._fallback_provider_meta("gpt-image-2.5-sunburst")
    assert auto_meta["provider"] == "openai-api"
    assert auto_meta["model_selection_policy"] == "openai-api-auto"
    assert auto_meta["model_configured_override"] == ""
    monkeypatch.setenv("OPENAI_IMAGE_FALLBACK_IMAGE_MODEL", "gpt-image-2")
    explicit_meta = renderer._fallback_provider_meta("gpt-image-2")
    assert explicit_meta["model_selection_policy"] == "openai-api-explicit"
    assert explicit_meta["model_configured_override"] == "gpt-image-2"

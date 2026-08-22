from pathlib import Path

app_path = Path('app.py')
source = app_path.read_text(encoding='utf-8')


def replace_once(old: str, new: str) -> None:
    global source
    count = source.count(old)
    if count != 1:
        raise SystemExit(f'expected exactly one occurrence, got {count}: {old[:120]!r}')
    source = source.replace(old, new, 1)


replace_once(
    'import httpx\nfrom fastapi import FastAPI, Header, HTTPException\n',
    'import httpx\nimport openai_fallback\nfrom fastapi import FastAPI, Header, HTTPException\n',
)
replace_once('APP_VERSION = "1.13.0"', 'APP_VERSION = "1.14.0"')
replace_once(
    '        "renderer": "codex-local", "app_version": APP_VERSION, "contract_version": CONTRACT_VERSION,\n'
    '        "customer_facing_generation": "codex_image_generation_only",\n'
    '        "local_visual_compositing_allowed": False,\n',
    '        "renderer": "codex-local", "app_version": APP_VERSION, "contract_version": CONTRACT_VERSION,\n'
    '        "customer_facing_generation": "codex_image_generation_only",\n'
    '        "image_provider_contract": "codex_primary_openai_api_quota_only_fallback",\n'
    '        "api_fallback_configured": openai_fallback.configured(),\n'
    '        "api_fallback_policy": "confirmed_codex_quota_only",\n'
    '        "local_visual_compositing_allowed": False,\n',
)
replace_once(
    'def _render(request: RenderRequest) -> tuple[bytes, str, str]:',
    'def _render(request: RenderRequest) -> tuple[bytes, str, str, dict[str, Any]]:',
)
replace_once(
    '            result = _run_codex_app_server(workspace, command_inputs, _prompt(request, prompt_context), timeout)\n'
    '            outputs = _new_outputs(workspace, before, command_inputs)\n'
    '            outputs.extend(path for path in result.saved_paths if path not in outputs)\n'
    '            if result.returncode != 0:\n'
    '                combined = ((result.stderr or "") + "\\n" + (result.stdout or "")).lower()\n'
    '                if any(term in combined for term in ("not logged in", "unauthorized", "401")):\n'
    '                    raise RuntimeError("codex_authentication_failed")\n'
    '                raise RuntimeError("codex_render_failed")\n'
    '            event_summary = _codex_event_summary(result.stdout)\n',
    '            render_prompt = _prompt(request, prompt_context)\n'
    '            result = _run_codex_app_server(workspace, command_inputs, render_prompt, timeout)\n'
    '            outputs = _new_outputs(workspace, before, command_inputs)\n'
    '            outputs.extend(path for path in result.saved_paths if path not in outputs)\n'
    '            if result.returncode != 0:\n'
    '                combined = ((result.stderr or "") + "\\n" + (result.stdout or "")).lower()\n'
    '                if any(term in combined for term in ("not logged in", "unauthorized", "401")):\n'
    '                    raise RuntimeError("codex_authentication_failed")\n'
    '                if openai_fallback.codex_quota_exhausted(combined):\n'
    '                    openai_fallback.mark_codex_quota_exhausted()\n'
    '                    if not openai_fallback.configured():\n'
    '                        raise RuntimeError("codex_quota_exhausted_api_fallback_not_configured")\n'
    '                    try:\n'
    '                        fallback_data, fallback_mime, fallback_model = openai_fallback.generate_image(\n'
    '                            render_prompt, command_inputs, timeout\n'
    '                        )\n'
    '                    except openai_fallback.OpenAIImageFallbackError as exc:\n'
    '                        raise RuntimeError(f"openai_quota_fallback_failed:{str(exc)}") from exc\n'
    '                    if len(fallback_data) > MAX_OUTPUT_BYTES:\n'
    '                        raise RuntimeError("output_too_large")\n'
    '                    sniffed_mime, _ = _sniff_image(fallback_data)\n'
    '                    if sniffed_mime != fallback_mime:\n'
    '                        fallback_mime = sniffed_mime\n'
    '                    fallback_digest = hashlib.sha256(fallback_data).hexdigest()\n'
    '                    provider_meta = {\n'
    '                        "provider": "openai-api",\n'
    '                        "fallback_used": True,\n'
    '                        "fallback_reason": "quota",\n'
    '                        "model": fallback_model,\n'
    '                    }\n'
    '                    with _REQUEST_DIGESTS_LOCK:\n'
    '                        if request_hash in _REQUEST_DIGESTS:\n'
    '                            _REQUEST_DIGESTS[request_hash].update({\n'
    '                                "status": "succeeded", "output_sha256": fallback_digest, **provider_meta\n'
    '                            })\n'
    '                    return fallback_data, fallback_mime, fallback_digest, provider_meta\n'
    '                raise RuntimeError("codex_render_failed")\n'
    '            event_summary = _codex_event_summary(result.stdout)\n',
)
replace_once(
    '                    _REQUEST_DIGESTS[request_hash].update({"status": "succeeded", "output_sha256": digest})\n'
    '            return data, mime, digest\n',
    '                    _REQUEST_DIGESTS[request_hash].update({\n'
    '                        "status": "succeeded", "output_sha256": digest,\n'
    '                        "provider": "codex-image", "fallback_used": False,\n'
    '                        "fallback_reason": "", "model": "gpt-image-2",\n'
    '                    })\n'
    '            return data, mime, digest, {\n'
    '                "provider": "codex-image", "fallback_used": False,\n'
    '                "fallback_reason": "", "model": "gpt-image-2",\n'
    '            }\n',
)
replace_once(
    '        data, mime, digest = _render(request)\n',
    '        data, mime, digest, provider_meta = _render(request)\n',
)
replace_once(
    '                    "output_sha256": digest,\n'
    '                    "result_path": result_path,\n'
    '                    "completed_at": time.time(),\n',
    '                    "output_sha256": digest,\n'
    '                    "result_path": result_path,\n'
    '                    "provider": str(provider_meta.get("provider") or ""),\n'
    '                    "fallback_used": bool(provider_meta.get("fallback_used")),\n'
    '                    "fallback_reason": str(provider_meta.get("fallback_reason") or ""),\n'
    '                    "model": str(provider_meta.get("model") or ""),\n'
    '                    "completed_at": time.time(),\n',
)
replace_once(
    '        "output_sha256": str(job.get("output_sha256") or job.get("digest") or ""),\n'
    '    })\n'
    '    payload["composition_mode"] = "codex_generated_final_raster"\n',
    '        "output_sha256": str(job.get("output_sha256") or job.get("digest") or ""),\n'
    '        "provider": str(job.get("provider") or "codex-image"),\n'
    '        "fallback_used": bool(job.get("fallback_used")),\n'
    '        "fallback_reason": str(job.get("fallback_reason") or ""),\n'
    '        "model": str(job.get("model") or "gpt-image-2"),\n'
    '    })\n'
    '    payload["composition_mode"] = "codex_generated_final_raster"\n',
)
replace_once(
    '        data, mime, digest = await asyncio.to_thread(_render, request)\n',
    '        data, mime, digest, provider_meta = await asyncio.to_thread(_render, request)\n',
)
replace_once(
    '        "X-Composition-Mode": "codex_generated_final_raster",\n'
    '    })\n',
    '        "X-Composition-Mode": "codex_generated_final_raster",\n'
    '        "X-Image-Provider": str(provider_meta.get("provider") or ""),\n'
    '        "X-Image-Fallback": "true" if provider_meta.get("fallback_used") else "false",\n'
    '        "X-Image-Fallback-Reason": str(provider_meta.get("fallback_reason") or ""),\n'
    '        "X-Image-Model": str(provider_meta.get("model") or ""),\n'
    '    })\n',
)
replace_once(
    '        "X-Render-Request-Sha256": str(job.get("request_hash") or ""),\n'
    '        "X-Composition-Mode": "codex_generated_final_raster",\n'
    '    })\n',
    '        "X-Render-Request-Sha256": str(job.get("request_hash") or ""),\n'
    '        "X-Composition-Mode": "codex_generated_final_raster",\n'
    '        "X-Image-Provider": str(job.get("provider") or "codex-image"),\n'
    '        "X-Image-Fallback": "true" if job.get("fallback_used") else "false",\n'
    '        "X-Image-Fallback-Reason": str(job.get("fallback_reason") or ""),\n'
    '        "X-Image-Model": str(job.get("model") or "gpt-image-2"),\n'
    '    })\n',
)
app_path.write_text(source, encoding='utf-8')

docker = Path('Dockerfile')
docker_source = docker.read_text(encoding='utf-8')
if docker_source.count('COPY app.py ./\n') != 1:
    raise SystemExit('Dockerfile app COPY invariant failed')
docker.write_text(docker_source.replace('COPY app.py ./\n', 'COPY app.py openai_fallback.py ./\n', 1), encoding='utf-8')

tests = Path('tests/test_app.py')
test_source = tests.read_text(encoding='utf-8')
if test_source.count('assert renderer.APP_VERSION == "1.13.0"') != 1:
    raise SystemExit('APP_VERSION test invariant failed')
test_source = test_source.replace('assert renderer.APP_VERSION == "1.13.0"', 'assert renderer.APP_VERSION == "1.14.0"', 1)
old_readiness = '''    status = renderer.readiness()\n    assert status["ready"] is False\n    assert status["customer_facing_generation"] == "codex_image_generation_only"\n'''
new_readiness = '''    monkeypatch.setenv("OPENAI_API_KEY", "configured-api-fallback")\n    status = renderer.readiness()\n    assert status["ready"] is False\n    assert status["api_fallback_configured"] is True\n    assert status["api_fallback_policy"] == "confirmed_codex_quota_only"\n    assert status["customer_facing_generation"] == "codex_image_generation_only"\n'''
if test_source.count(old_readiness) != 1:
    raise SystemExit('readiness test invariant failed')
test_source = test_source.replace(old_readiness, new_readiness, 1)
marker = '\ndef test_completed_matching_async_requests_are_reused(monkeypatch):\n'
if marker not in test_source:
    raise SystemExit('test insertion marker missing')
added = r'''

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
'''
test_source = test_source.replace(marker, added + marker, 1)
tests.write_text(test_source, encoding='utf-8')

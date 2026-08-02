from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

import gateway


def auth() -> dict[str, str]:
    return {"Authorization": "Bearer sk-fallback-test"}


def client_for(monkeypatch, codex_call, api_call) -> TestClient:
    monkeypatch.setattr(gateway, "call_codex", codex_call)
    monkeypatch.setattr(gateway, "call_openai", api_call)
    monkeypatch.setattr(gateway, "OPENAI_KEY", "")
    monkeypatch.setattr(gateway, "GATEWAY_TOKEN", "")
    monkeypatch.setattr(gateway, "ALLOW_INBOUND_KEY", True)
    monkeypatch.setattr(gateway, "circuit", gateway.Circuit())
    return TestClient(gateway.app)


def test_codex_primary_success(monkeypatch):
    calls: list[str] = []

    async def codex_call(endpoint, payload, request_id):
        calls.append(f"codex:{endpoint}")
        assert payload["model"] == "gpt-5.6-luna"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    async def api_call(key, endpoint, payload, request_id):
        raise AssertionError("API fallback must not be called")

    with client_for(monkeypatch, codex_call, api_call) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth(),
            json={"model": "luna-auto", "messages": []},
        )

    assert response.status_code == 200
    assert response.headers["x-luna-gateway-provider"] == "codex"
    assert response.headers["x-luna-gateway-fallback"] == "false"
    assert calls == ["codex:chat"]


def test_quota_exhaustion_falls_back_and_opens_circuit(monkeypatch):
    calls: list[str] = []

    async def codex_call(endpoint, payload, request_id):
        calls.append("codex")
        return httpx.Response(
            429,
            json={"error": {"message": "Codex usage limit reached"}},
        )

    async def api_call(key, endpoint, payload, request_id):
        calls.append("api")
        assert key == "sk-fallback-test"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "api"}}]},
        )

    with client_for(monkeypatch, codex_call, api_call) as client:
        first = client.post(
            "/v1/chat/completions",
            headers=auth(),
            json={"model": "gpt-5.6-luna", "messages": []},
        )
        second = client.post(
            "/v1/chat/completions",
            headers=auth(),
            json={"model": "gpt-5.6-luna", "messages": []},
        )

    assert first.status_code == 200
    assert first.headers["x-luna-gateway-provider"] == "openai-api"
    assert first.headers["x-luna-gateway-fallback-reason"] == "quota"
    assert second.headers["x-luna-gateway-provider"] == "openai-api"
    assert second.headers["x-luna-gateway-fallback-reason"].startswith("circuit_open:quota")
    assert calls == ["codex", "api", "api"]


def test_non_retryable_request_error_does_not_fallback(monkeypatch):
    calls: list[str] = []

    async def codex_call(endpoint, payload, request_id):
        calls.append("codex")
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    async def api_call(key, endpoint, payload, request_id):
        calls.append("api")
        raise AssertionError("API fallback must not be called for provider 400")

    with client_for(monkeypatch, codex_call, api_call) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth(),
            json={"model": "gpt-5.6-luna", "messages": []},
        )

    assert response.status_code == 400
    assert response.headers["x-luna-gateway-provider"] == "codex"
    assert calls == ["codex"]


def test_invalid_structured_output_uses_api_fallback(monkeypatch):
    async def codex_call(endpoint, payload, request_id):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "not json"}}]},
        )

    async def api_call(key, endpoint, payload, request_id):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"ok": True}),
                        }
                    }
                ]
            },
        )

    with client_for(monkeypatch, codex_call, api_call) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth(),
            json={
                "model": "gpt-5.6-luna",
                "messages": [],
                "response_format": {"type": "json_object"},
            },
        )

    assert response.status_code == 200
    assert response.headers["x-luna-gateway-provider"] == "openai-api"
    assert response.headers["x-luna-gateway-fallback-reason"] == "invalid_success"


def test_public_hostname_is_rejected(monkeypatch):
    async def unused(*args, **kwargs):
        raise AssertionError("provider must not be called")

    with client_for(monkeypatch, unused, unused) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={**auth(), "Host": "fwxnnc9hd9288dt66wqte5x2.luxeillum.com"},
            json={"model": "gpt-5.6-luna", "messages": []},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "internal_gateway_only"

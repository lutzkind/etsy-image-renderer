from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

import gateway


def client_for(handler):
    gateway.CODEX_KEY = "codex-key"
    gateway.OPENAI_KEY = ""
    gateway.GATEWAY_TOKEN = ""
    gateway.ALLOW_INBOUND_KEY = True
    gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
    gateway.circuit = gateway.Circuit()
    return TestClient(gateway.app)


def auth():
    return {"Authorization": "Bearer sk-fallback-test"}


def test_codex_primary_success():
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    with client_for(handler) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth(),
            json={"model": "gpt-5.6-luna", "messages": []},
        )

    assert response.status_code == 200
    assert response.headers["x-luna-gateway-provider"] == "codex"
    assert response.headers["x-luna-gateway-fallback"] == "false"
    assert calls == ["http://codex-upstream:18080/v1/chat/completions"]


def test_quota_exhaustion_falls_back_and_opens_circuit():
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        if request.url.host == "codex-upstream":
            return httpx.Response(429, json={"error": {"message": "Codex usage limit reached"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "api"}}]})

    with client_for(handler) as client:
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
    assert second.headers["x-luna-gateway-fallback-reason"].startswith("circuit_open:quota")
    assert calls == [
        "http://codex-upstream:18080/v1/chat/completions",
        "https://api.openai.com/v1/chat/completions",
        "https://api.openai.com/v1/chat/completions",
    ]


def test_non_retryable_request_error_does_not_fallback():
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    with client_for(handler) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth(),
            json={"model": "gpt-5.6-luna", "messages": []},
        )

    assert response.status_code == 400
    assert response.headers["x-luna-gateway-provider"] == "codex"
    assert calls == ["http://codex-upstream:18080/v1/chat/completions"]


def test_invalid_structured_output_uses_api_fallback():
    def handler(request: httpx.Request):
        if request.url.host == "codex-upstream":
            return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"ok": True})}}]},
        )

    with client_for(handler) as client:
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

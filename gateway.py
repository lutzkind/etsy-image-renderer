from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
import uuid
from collections import deque
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from jsonschema import ValidationError, validate as validate_schema

Endpoint = Literal["chat", "responses"]
CODEX_URL = os.getenv("CODEX_UPSTREAM_URL", "http://codex-upstream:18080/v1").rstrip("/")
CODEX_KEY = os.getenv("CODEX_UPSTREAM_API_KEY", "").strip()
OPENAI_URL = os.getenv("OPENAI_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
GATEWAY_TOKEN = os.getenv("LUNA_GATEWAY_TOKEN", "").strip()
ALLOW_INBOUND_KEY = os.getenv("ALLOW_INBOUND_FALLBACK_KEY", "false").lower() in {"1", "true", "yes", "on"}
ALLOWED_MODELS = {item.strip() for item in os.getenv("ALLOWED_MODELS", "gpt-5.6-luna,luna-auto").split(",") if item.strip()}
MODEL_ALIASES = json.loads(os.getenv("MODEL_ALIASES_JSON", '{"luna-auto":"gpt-5.6-luna"}'))
TIMEOUT = float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "180"))
MAX_BODY = int(os.getenv("MAX_BODY_BYTES", str(2 * 1024 * 1024)))
MAX_CONCURRENCY = max(1, int(os.getenv("MAX_CONCURRENCY", "4")))
QUOTA_OPEN_SECONDS = int(os.getenv("QUOTA_OPEN_SECONDS", "1800"))
TRANSIENT_OPEN_SECONDS = int(os.getenv("TRANSIENT_OPEN_SECONDS", "900"))
TRANSIENT_THRESHOLD = int(os.getenv("TRANSIENT_FAILURE_THRESHOLD", "3"))
TRANSIENT_WINDOW = int(os.getenv("TRANSIENT_FAILURE_WINDOW_SECONDS", "300"))


class Circuit:
    def __init__(self) -> None:
        self.open_until = 0.0
        self.reason: str | None = None
        self.failures: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def skip(self) -> tuple[bool, str | None]:
        async with self.lock:
            if self.open_until > time.time():
                return True, self.reason
            self.open_until = 0.0
            self.reason = None
            return False, None

    async def success(self) -> None:
        async with self.lock:
            self.open_until = 0.0
            self.reason = None
            self.failures.clear()

    async def fail(self, reason: str) -> None:
        async with self.lock:
            now = time.time()
            if reason in {"quota", "auth"}:
                self.open_until = now + (QUOTA_OPEN_SECONDS if reason == "quota" else 300)
                self.reason = reason
                self.failures.clear()
                return
            if reason not in {"rate_limit", "timeout", "network", "upstream_5xx", "invalid_success"}:
                return
            cutoff = now - TRANSIENT_WINDOW
            while self.failures and self.failures[0] < cutoff:
                self.failures.popleft()
            self.failures.append(now)
            if len(self.failures) >= TRANSIENT_THRESHOLD:
                self.open_until = now + TRANSIENT_OPEN_SECONDS
                self.reason = reason
                self.failures.clear()

    async def state(self) -> dict[str, Any]:
        skip, reason = await self.skip()
        return {"open": skip, "reason": reason, "open_until": self.open_until if skip else None}


app = FastAPI(title="Windmill Luna Gateway", version="1.0.0")
client = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT), follow_redirects=False)
circuit = Circuit()
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)


@app.on_event("shutdown")
async def shutdown() -> None:
    await client.aclose()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "codex_configured": bool(CODEX_KEY),
        "api_fallback_configured": bool(OPENAI_KEY or ALLOW_INBOUND_KEY),
        "fallback_key_mode": "configured" if OPENAI_KEY else ("inbound_bearer" if ALLOW_INBOUND_KEY else "disabled"),
        "circuit": await circuit.state(),
    }


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Response:
    return await handle(request, "chat")


@app.post("/v1/responses")
async def responses(request: Request) -> Response:
    return await handle(request, "responses")


async def handle(request: Request, endpoint: Endpoint) -> Response:
    bearer = authenticate(request)
    payload = await body(request)
    model = payload.get("model")
    if not isinstance(model, str) or model not in ALLOWED_MODELS:
        raise HTTPException(400, "model_not_allowed")
    if payload.get("stream") is True:
        raise HTTPException(400, "streaming_not_supported")
    payload["model"] = MODEL_ALIASES.get(model, model)
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    fallback_key = bearer if ALLOW_INBOUND_KEY else OPENAI_KEY

    async with semaphore:
        skip, skip_reason = await circuit.skip()
        if not skip:
            primary = await call(CODEX_URL, CODEX_KEY, endpoint, payload, request_id)
            reason = failure_reason(primary)
            if reason is None and primary is not None:
                invalid = invalid_success(primary, endpoint, payload)
                if invalid is None:
                    await circuit.success()
                    return relay(primary, "codex", False, None, request_id)
                reason = "invalid_success"
            if reason is None and primary is not None:
                return relay(primary, "codex", False, None, request_id)
            await circuit.fail(reason or "network")
            fallback_reason = reason or "network"
        else:
            fallback_reason = f"circuit_open:{skip_reason or 'unknown'}"

        fallback = await call(OPENAI_URL, fallback_key, endpoint, payload, request_id)
        if fallback is None:
            return Response(
                json.dumps({"error": {"message": "Both providers unavailable", "type": "gateway_provider_error"}}),
                502,
                headers=headers("none", True, fallback_reason, request_id),
                media_type="application/json",
            )
        return relay(fallback, "openai-api", True, fallback_reason, request_id)


def authenticate(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "unauthorized")
    token = token.strip()
    if GATEWAY_TOKEN and not hmac.compare_digest(token, GATEWAY_TOKEN):
        raise HTTPException(401, "unauthorized")
    if not GATEWAY_TOKEN and not ALLOW_INBOUND_KEY:
        raise HTTPException(503, "gateway_not_configured")
    return token


async def body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "request_too_large")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "invalid_json") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "request_body_must_be_object")
    return value


async def call(base: str, key: str, endpoint: Endpoint, payload: dict[str, Any], request_id: str) -> httpx.Response | None:
    if not key:
        return None
    path = "/chat/completions" if endpoint == "chat" else "/responses"
    try:
        return await client.post(
            base + path,
            headers={"Authorization": f"Bearer {key}", "X-Request-ID": request_id},
            json=payload,
        )
    except httpx.HTTPError:
        return None


def failure_reason(response: httpx.Response | None) -> str | None:
    if response is None:
        return "network"
    status = response.status_code
    text = response.text[:4000].lower()
    if status in {401, 403}:
        return "auth"
    if status == 429:
        quota_words = ("usage limit", "quota", "plan limit", "limit reached", "insufficient_quota", "codex usage")
        return "quota" if any(word in text for word in quota_words) else "rate_limit"
    if status == 408:
        return "timeout"
    if status in {500, 502, 503, 504}:
        return "upstream_5xx"
    return None


def invalid_success(response: httpx.Response, endpoint: Endpoint, request: dict[str, Any]) -> str | None:
    if not 200 <= response.status_code < 300:
        return None
    try:
        data = response.json()
    except ValueError:
        return "non_json_success"
    if not isinstance(data, dict):
        return "non_object_success"
    text: str | None = None
    if endpoint == "chat":
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return "missing_choices"
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            return "missing_message"
        content = message.get("content")
        text = content if isinstance(content, str) else None
        if text is None and not message.get("tool_calls") and not message.get("function_call"):
            return "empty_chat_success"
    else:
        output = data.get("output")
        if not isinstance(output, list):
            return "missing_output"
        text = data.get("output_text") if isinstance(data.get("output_text"), str) else output_text(output)
        if not output and not text:
            return "empty_response_success"
    return structured_error(request, text)


def output_text(output: list[Any]) -> str | None:
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "".join(parts) if parts else None


def structured_error(request: dict[str, Any], text: str | None) -> str | None:
    schema = None
    requires_json = False
    fmt = request.get("response_format")
    if isinstance(fmt, dict) and fmt.get("type") in {"json_object", "json_schema"}:
        requires_json = True
        nested = fmt.get("json_schema")
        if isinstance(nested, dict) and isinstance(nested.get("schema"), dict):
            schema = nested["schema"]
    text_cfg = request.get("text")
    if isinstance(text_cfg, dict) and isinstance(text_cfg.get("format"), dict):
        nested = text_cfg["format"]
        if nested.get("type") in {"json_object", "json_schema"}:
            requires_json = True
        if isinstance(nested.get("schema"), dict):
            schema = nested["schema"]
    if not requires_json:
        return None
    if not text:
        return "empty_structured_output"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return "invalid_structured_json"
    if schema:
        try:
            validate_schema(parsed, schema)
        except ValidationError:
            return "structured_schema_failure"
    return None


def headers(provider: str, fallback: bool, reason: str | None, request_id: str) -> dict[str, str]:
    result = {
        "X-Luna-Gateway-Provider": provider,
        "X-Luna-Gateway-Fallback": "true" if fallback else "false",
        "X-Request-ID": request_id,
        "Cache-Control": "no-store",
    }
    if reason:
        result["X-Luna-Gateway-Fallback-Reason"] = reason
    return result


def relay(upstream: httpx.Response, provider: str, fallback: bool, reason: str | None, request_id: str) -> Response:
    return Response(
        upstream.content,
        upstream.status_code,
        headers=headers(provider, fallback, reason, request_id),
        media_type=upstream.headers.get("content-type", "application/json").split(";", 1)[0],
    )

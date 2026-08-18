from __future__ import annotations

import base64
import binascii
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx


MAX_FALLBACK_INPUT_BYTES = 16 * 1024 * 1024
MAX_FALLBACK_OUTPUT_BYTES = 25 * 1024 * 1024
_DEFAULT_RESPONSES_MODEL = "gpt-5"
_DEFAULT_IMAGE_MODEL = "gpt-image-2"
_DEFAULT_TIMEOUT_SECONDS = 900
_DEFAULT_CIRCUIT_SECONDS = 1800

_QUOTA_LOCK = threading.Lock()
_QUOTA_BLOCKED_UNTIL = 0.0


class OpenAIImageFallbackError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def responses_model() -> str:
    return os.environ.get("OPENAI_IMAGE_FALLBACK_MODEL", _DEFAULT_RESPONSES_MODEL).strip() or _DEFAULT_RESPONSES_MODEL


def image_model() -> str:
    return os.environ.get("OPENAI_IMAGE_FALLBACK_IMAGE_MODEL", _DEFAULT_IMAGE_MODEL).strip() or _DEFAULT_IMAGE_MODEL


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def timeout_seconds(request_timeout: int | None = None) -> int:
    configured_timeout = _bounded_int("OPENAI_IMAGE_FALLBACK_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS, 60, 1800)
    if request_timeout is None:
        return configured_timeout
    return max(60, min(int(request_timeout), configured_timeout, 1800))


def quota_circuit_seconds() -> int:
    return _bounded_int("CODEX_QUOTA_CIRCUIT_SECONDS", _DEFAULT_CIRCUIT_SECONDS, 60, 21600)


def codex_quota_exhausted(raw: str) -> bool:
    value = str(raw or "").lower()
    markers = (
        "usage limit",
        "usage_limit",
        "insufficient_quota",
        "quota exhausted",
        "quota exceeded",
        "plan limit",
        "weekly limit",
        "weighted tokens left",
        "you've hit your usage limit",
        "you have hit your usage limit",
        "out of quota",
    )
    if any(marker in value for marker in markers):
        return True
    return "quota" in value and any(marker in value for marker in ("exhaust", "exceed", "limit", "remaining", "available"))


def mark_codex_quota_exhausted(now: float | None = None) -> float:
    global _QUOTA_BLOCKED_UNTIL
    current = time.time() if now is None else float(now)
    blocked_until = current + quota_circuit_seconds()
    with _QUOTA_LOCK:
        _QUOTA_BLOCKED_UNTIL = max(_QUOTA_BLOCKED_UNTIL, blocked_until)
        return _QUOTA_BLOCKED_UNTIL


def quota_circuit_open(now: float | None = None) -> bool:
    current = time.time() if now is None else float(now)
    with _QUOTA_LOCK:
        return _QUOTA_BLOCKED_UNTIL > current


def reset_quota_circuit() -> None:
    global _QUOTA_BLOCKED_UNTIL
    with _QUOTA_LOCK:
        _QUOTA_BLOCKED_UNTIL = 0.0


def _mime_for_bytes(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise OpenAIImageFallbackError("openai_image_fallback_unsupported_output")


def _data_url(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > MAX_FALLBACK_INPUT_BYTES:
        raise OpenAIImageFallbackError("openai_image_fallback_input_too_large")
    mime = _mime_for_bytes(data)
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _base_url() -> str:
    value = os.environ.get("OPENAI_API_BASE_URL", "https://api.openai.com/v1").strip()
    return (value or "https://api.openai.com/v1").rstrip("/")


def _tool_config(has_inputs: bool) -> dict[str, Any]:
    quality = os.environ.get("OPENAI_IMAGE_FALLBACK_QUALITY", "high").strip().lower() or "high"
    if quality not in {"low", "medium", "high", "auto"}:
        quality = "high"
    size = os.environ.get("OPENAI_IMAGE_FALLBACK_SIZE", "auto").strip().lower() or "auto"
    if size not in {"1024x1024", "1024x1536", "1536x1024", "auto"}:
        size = "auto"
    tool: dict[str, Any] = {
        "type": "image_generation",
        "model": image_model(),
        "action": "edit" if has_inputs else "generate",
        "quality": quality,
        "size": size,
        "output_format": "png",
    }
    if has_inputs:
        tool["input_fidelity"] = "high"
    return tool


def generate_image(prompt: str, inputs: list[Path], request_timeout: int | None = None) -> tuple[bytes, str, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise OpenAIImageFallbackError("openai_image_fallback_not_configured")

    content: list[dict[str, Any]] = [{"type": "input_text", "text": str(prompt)}]
    content.extend({"type": "input_image", "image_url": _data_url(path), "detail": "high"} for path in inputs)
    tool = _tool_config(bool(inputs))
    payload = {
        "model": responses_model(),
        "input": [{"role": "user", "content": content}],
        "tools": [tool],
        "tool_choice": {"type": "image_generation"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    project = os.environ.get("OPENAI_PROJECT", "").strip()
    organization = os.environ.get("OPENAI_ORGANIZATION", "").strip()
    if project:
        headers["OpenAI-Project"] = project
    if organization:
        headers["OpenAI-Organization"] = organization

    try:
        with httpx.Client(timeout=timeout_seconds(request_timeout)) as client:
            response = client.post(f"{_base_url()}/responses", headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise OpenAIImageFallbackError("openai_image_fallback_transport_failed") from exc

    if response.status_code >= 400:
        raise OpenAIImageFallbackError(f"openai_image_fallback_http_{response.status_code}")

    try:
        body = response.json()
    except ValueError as exc:
        raise OpenAIImageFallbackError("openai_image_fallback_invalid_json") from exc

    outputs = body.get("output") if isinstance(body, dict) else None
    if not isinstance(outputs, list):
        raise OpenAIImageFallbackError("openai_image_fallback_output_missing")
    encoded_results = [
        item.get("result")
        for item in outputs
        if isinstance(item, dict) and item.get("type") == "image_generation_call" and isinstance(item.get("result"), str)
    ]
    if len(encoded_results) != 1:
        raise OpenAIImageFallbackError("openai_image_fallback_output_ambiguous" if encoded_results else "openai_image_fallback_output_missing")

    try:
        data = base64.b64decode(encoded_results[0], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OpenAIImageFallbackError("openai_image_fallback_invalid_base64") from exc
    if len(data) > MAX_FALLBACK_OUTPUT_BYTES:
        raise OpenAIImageFallbackError("openai_image_fallback_output_too_large")
    mime = _mime_for_bytes(data)
    return data, mime, image_model()

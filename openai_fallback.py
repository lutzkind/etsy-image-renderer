from __future__ import annotations

import base64
import binascii
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx

MAX_FALLBACK_INPUT_BYTES = 16 * 1024 * 1024
MAX_FALLBACK_OUTPUT_BYTES = 25 * 1024 * 1024
_DEFAULT_RESPONSES_MODEL = "gpt-5"
# Last-known-compatible fallback baseline.  The normal production path is
# Codex's built-in image generation and never uses this module.  The paid API
# fallback is explicitly authorized and normally resolves its image model from
# the account's live catalog; this baseline is only used when a catalog lookup
# is temporarily unavailable and no last successful resolution exists.  It
# must never regress to an obsolete Image 1 family model.
_BASELINE_IMAGE_MODEL = "gpt-image-2.5-sunburst"
_DEFAULT_TIMEOUT_SECONDS = 900
_DEFAULT_CIRCUIT_SECONDS = 1800

# Automatic image-model resolution.  ``OPENAI_IMAGE_FALLBACK_IMAGE_MODEL`` may
# hold an explicit exact model (emergency rollback/testing) or ``auto``
# (equivalently: unset/empty).  In automatic mode the account model catalog is
# queried at most once per bounded TTL and the newest stable full-capability
# GPT Image release is selected without any source-code change for new
# releases.
AUTO_IMAGE_MODEL = "auto"
_DEFAULT_CATALOG_TTL_SECONDS = 3600
_DEFAULT_CATALOG_TIMEOUT_SECONDS = 10
_MIN_CATALOG_TTL_SECONDS = 60
_MAX_CATALOG_TTL_SECONDS = 86400
_MIN_CATALOG_TIMEOUT_SECONDS = 2
_MAX_CATALOG_TIMEOUT_SECONDS = 60

# Explicit operator authorization for *paid* OpenAI image API spend.  The
# default MUST remain false: Codex quota exhaustion never authorizes paid API
# generation.  Only an approved operator/test/emergency override may flip it.
PAID_FALLBACK_ENV = "ALLOW_PAID_OPENAI_IMAGE_FALLBACK"
_IMAGE_MODEL_ENV = "OPENAI_IMAGE_FALLBACK_IMAGE_MODEL"
_TRUTHY = {"1", "true", "yes", "on", "enabled"}

# GPT Image model identifiers.  The renderer never needs a per-release mapping
# entry: the version is parsed numerically (so 2.10 > 2.9) and a future normal
# release such as ``gpt-image-3`` or ``gpt-image-3-<variant>`` is understood
# automatically.  Deprecated ``chatgpt-image-*`` aliases and dall-e models do
# not match this contract and are never selected automatically.
_GPT_IMAGE_ID = re.compile(
    r"^gpt-image-(?P<version>\d+(?:\.\d+)*)"
    r"(?:-(?P<variant>[a-z][a-z0-9]*))?"
    r"(?:-(?P<snapshot>\d{4}-\d{2}-\d{2}))?$"
)
# Reduced-capability / non-stable identifiers never become the premium
# default.  They remain usable only through an explicit operator pin.
_REDUCED_CAPABILITY_TOKENS = ("mini", "preview", "experimental", "alpha", "beta")
# Documented same-release quality siblings.  Sunburst is the most capable
# image generation/editing model; Flare is the faster everyday model.  An
# undecorated or unknown variant ranks at the mainstream tier and is decided
# deterministically by release version, then created timestamp, then id.
_VARIANT_QUALITY_TIER = {"sunburst": 30, "": 20, "flare": 10}
_DEFAULT_VARIANT_QUALITY_TIER = 20


class OpenAIImageFallbackError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageModelResolution:
    model: str
    policy: str
    source: str
    configured_override: str
    catalog_error: str = ""
    fetched_at: float = 0.0


_MODEL_LOCK = threading.Lock()
_MODEL_STATE: dict[str, Any] = {
    "model": "",
    "fetched_at": 0.0,
    "catalog_error": "",
    "catalog_size": 0,
}

_QUOTA_LOCK = threading.Lock()
_QUOTA_BLOCKED_UNTIL = 0.0


def configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def paid_fallback_authorized() -> bool:
    """Return whether paid OpenAI API image generation is explicitly allowed."""
    return os.environ.get(PAID_FALLBACK_ENV, "").strip().lower() in _TRUTHY


def authorization_source() -> str:
    return PAID_FALLBACK_ENV if paid_fallback_authorized() else "none"


def paid_fallback_policy() -> str:
    return "explicit_paid_authorization_only"


def responses_model() -> str:
    return os.environ.get("OPENAI_IMAGE_FALLBACK_MODEL", _DEFAULT_RESPONSES_MODEL).strip() or _DEFAULT_RESPONSES_MODEL


def image_model_setting() -> str:
    """Return the raw image-model setting (``""`` when unset)."""
    return os.environ.get(_IMAGE_MODEL_ENV, "").strip()


def image_model_policy() -> str:
    """Return ``explicit`` for an operator pin and ``auto`` otherwise."""
    setting = image_model_setting()
    return "explicit" if setting and setting.lower() != AUTO_IMAGE_MODEL else "auto"


def configured_override() -> str:
    """Return the explicit operator pin, or ``""`` in automatic mode."""
    return image_model_setting() if image_model_policy() == "explicit" else ""


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def catalog_ttl_seconds() -> int:
    return _bounded_int(
        "OPENAI_IMAGE_MODEL_CATALOG_TTL_SECONDS",
        _DEFAULT_CATALOG_TTL_SECONDS,
        _MIN_CATALOG_TTL_SECONDS,
        _MAX_CATALOG_TTL_SECONDS,
    )


def catalog_timeout_seconds() -> int:
    return _bounded_int(
        "OPENAI_IMAGE_MODEL_CATALOG_TIMEOUT_SECONDS",
        _DEFAULT_CATALOG_TIMEOUT_SECONDS,
        _MIN_CATALOG_TIMEOUT_SECONDS,
        _MAX_CATALOG_TIMEOUT_SECONDS,
    )


def timeout_seconds(request_timeout: int | None = None) -> int:
    configured_timeout = _bounded_int("OPENAI_IMAGE_FALLBACK_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS, 60, 1800)
    if request_timeout is None:
        return configured_timeout
    return max(60, min(int(request_timeout), configured_timeout, 1800))


def quota_circuit_seconds() -> int:
    return _bounded_int("CODEX_QUOTA_CIRCUIT_SECONDS", _DEFAULT_CIRCUIT_SECONDS, 60, 21600)


def codex_quota_exhausted(raw: str) -> bool:
    """Return True only for positive evidence that Codex usage is exhausted.

    Generic 429/rate-limit, timeout, capacity, auth, or merely mentioning a
    quota/plan/usage limit is intentionally insufficient. This predicate is the
    spend boundary for the OpenAI API image fallback.
    """
    value = str(raw or "").lower()
    strong_markers = (
        "insufficient_quota",
        "quota exhausted",
        "quota exceeded",
        "out of quota",
        "you've hit your usage limit",
        "you have hit your usage limit",
        "usage limit reached",
        "reached your usage limit",
        "weekly limit reached",
        "reached your weekly limit",
        "plan limit reached",
        "plan limit exceeded",
    )
    if any(marker in value for marker in strong_markers):
        return True

    used_100 = bool(re.search(r"x-codex-primary-used-percent[^0-9]{0,12}100(?:\.0+)?\b", value))
    no_credits = bool(re.search(r"x-codex-credits-has-credits[^a-z0-9]{0,12}false\b", value))
    zero_balance = bool(re.search(r"x-codex-credits-balance[^0-9-]{0,12}0(?:\.0+)?\b", value))
    if used_100 and (no_credits or zero_balance):
        return True

    zero_remaining_patterns = (
        r"quota\s+(?:remaining|left)[^0-9-]{0,12}0(?:\.0+)?\b",
        r"(?:weighted\s+)?tokens\s+left[^0-9-]{0,12}0(?:\.0+)?\b",
        r"quota\s+available[^a-z0-9-]{0,12}(?:false|0(?:\.0+)?)\b",
    )
    return any(re.search(pattern, value) for pattern in zero_remaining_patterns)


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


def reset_image_model_state() -> None:
    """Reset the in-process model resolution cache (tests/operator tooling)."""
    with _MODEL_LOCK:
        _MODEL_STATE.update({"model": "", "fetched_at": 0.0, "catalog_error": "", "catalog_size": 0})


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
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _base_url() -> str:
    return (os.environ.get("OPENAI_API_BASE_URL", "https://api.openai.com/v1").strip() or "https://api.openai.com/v1").rstrip("/")


def _catalog_model_id(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("id") or "").strip()
    return ""


def _catalog_model_created(item: Any) -> int:
    if isinstance(item, dict) and isinstance(item.get("created"), (int, float)) and not isinstance(item.get("created"), bool):
        return int(item["created"])
    return 0


def _parse_gpt_image_id(model_id: str) -> re.Match[str] | None:
    return _GPT_IMAGE_ID.match(str(model_id or "").strip().lower())


def select_best_image_model(models: Iterable[Any]) -> str:
    """Select the newest stable full-capability GPT Image model.

    Deterministic and network-free.  Release versions are compared
    numerically (2.10 > 2.9), not lexicographically.  Dated snapshots are
    skipped when the undated stable alias exists; reduced-capability,
    preview/experimental, deprecated, and non-GPT-Image identifiers are never
    selected.  ``created`` is only a supporting tiebreak, never the semantic
    definition of "best".
    """
    entries: list[dict[str, Any]] = []
    for item in models or []:
        model_id = _catalog_model_id(item)
        if not model_id:
            continue
        lowered = model_id.lower()
        match = _parse_gpt_image_id(lowered)
        if match is None:
            continue
        if any(token in lowered for token in _REDUCED_CAPABILITY_TOKENS):
            continue
        if isinstance(item, dict) and item.get("deprecated") is True:
            continue
        entries.append({
            "id": model_id,
            "version": match.group("version"),
            "variant": match.group("variant") or "",
            "snapshot": match.group("snapshot") or "",
            "created": _catalog_model_created(item),
        })
    if not entries:
        return ""
    undated: set[tuple[str, str]] = {
        (entry["version"], entry["variant"]) for entry in entries if not entry["snapshot"]
    }
    eligible = [
        entry for entry in entries
        if not entry["snapshot"] or (entry["version"], entry["variant"]) not in undated
    ]
    eligible.sort(
        key=lambda entry: (
            tuple(int(part) for part in str(entry["version"]).split(".")),
            _VARIANT_QUALITY_TIER.get(str(entry["variant"]), _DEFAULT_VARIANT_QUALITY_TIER),
            int(entry["created"]),
            str(entry["id"]).lower(),
        ),
        reverse=True,
    )
    return str(eligible[0]["id"])


def _catalog_headers() -> dict[str, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise OpenAIImageFallbackError("openai_image_model_catalog_not_configured")
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    project = os.environ.get("OPENAI_PROJECT", "").strip()
    organization = os.environ.get("OPENAI_ORGANIZATION", "").strip()
    if project:
        headers["OpenAI-Project"] = project
    if organization:
        headers["OpenAI-Organization"] = organization
    return headers


def _fetch_image_model_catalog() -> list[Any]:
    """Fetch the account's authoritative model inventory (zero-cost)."""
    headers = _catalog_headers()
    try:
        with httpx.Client(timeout=catalog_timeout_seconds()) as client:
            response = client.get(f"{_base_url()}/models", headers=headers)
    except httpx.HTTPError as exc:
        raise OpenAIImageFallbackError("openai_image_model_catalog_transport_failed") from exc
    if response.status_code >= 400:
        raise OpenAIImageFallbackError(f"openai_image_model_catalog_http_{response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise OpenAIImageFallbackError("openai_image_model_catalog_invalid_json") from exc
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise OpenAIImageFallbackError("openai_image_model_catalog_invalid")
    return data


def _cache_resolution(model: str, catalog_size: int, catalog_error: str) -> None:
    with _MODEL_LOCK:
        _MODEL_STATE.update({
            "model": model,
            "fetched_at": time.time(),
            "catalog_error": catalog_error,
            "catalog_size": catalog_size,
        })


def resolve_image_model(*, allow_network: bool = True, force_refresh: bool = False) -> ImageModelResolution:
    """Resolve the fallback image model safely and deterministically.

    Explicit operator pin wins.  Otherwise automatic mode uses the bounded
    catalog cache, then a live (zero-cost) catalog lookup, then the last
    successfully resolved model, then the well-defined baseline.  Resolution
    never raises and never turns catalog uncertainty into spend authorization.
    """
    setting = image_model_setting()
    if image_model_policy() == "explicit":
        return ImageModelResolution(
            model=setting,
            policy="explicit",
            source="explicit",
            configured_override=setting,
        )

    now = time.time()
    with _MODEL_LOCK:
        cached_model = str(_MODEL_STATE.get("model") or "")
        fetched_at = float(_MODEL_STATE.get("fetched_at") or 0.0)
        catalog_error = str(_MODEL_STATE.get("catalog_error") or "")
    cache_fresh = bool(cached_model) and (now - fetched_at) < catalog_ttl_seconds()
    if cache_fresh and not force_refresh:
        return ImageModelResolution(
            model=cached_model,
            policy="auto",
            source="cache",
            configured_override="",
            catalog_error=catalog_error,
            fetched_at=fetched_at,
        )

    error = ""
    if allow_network:
        try:
            catalog = _fetch_image_model_catalog()
            selected = select_best_image_model(catalog)
            if not selected:
                raise OpenAIImageFallbackError("openai_image_model_catalog_empty")
            _cache_resolution(selected, len(catalog), "")
            return ImageModelResolution(
                model=selected,
                policy="auto",
                source="catalog",
                configured_override="",
                fetched_at=time.time(),
            )
        except Exception as exc:  # noqa: BLE001 - resolution must never fail a render
            error = str(exc)[:200]

    if cached_model:
        return ImageModelResolution(
            model=cached_model,
            policy="auto",
            source="last_known",
            configured_override="",
            catalog_error=error,
            fetched_at=fetched_at,
        )
    return ImageModelResolution(
        model=_BASELINE_IMAGE_MODEL,
        policy="auto",
        source="baseline",
        configured_override="",
        catalog_error=error,
    )


def image_model() -> str:
    """Return the resolved fallback image model without external I/O."""
    return resolve_image_model(allow_network=False).model


def image_model_resolution_status() -> dict[str, Any]:
    """Return non-secret observability for the fallback model policy.

    This never performs network I/O, so it is safe for the fast ``/health``
    probe; a live resolution happens only when an authorized fallback is about
    to run (or when an operator explicitly refreshes it).
    """
    resolution = resolve_image_model(allow_network=False)
    with _MODEL_LOCK:
        fetched_at = float(_MODEL_STATE.get("fetched_at") or 0.0)
        catalog_size = int(_MODEL_STATE.get("catalog_size") or 0)
    age = max(0.0, time.time() - fetched_at) if fetched_at else None
    return {
        "policy": resolution.policy,
        "setting": image_model_setting(),
        "configured_override": resolution.configured_override,
        "resolved_model": resolution.model,
        "resolution_source": resolution.source,
        "catalog_error": resolution.catalog_error,
        "catalog_age_seconds": age,
        "catalog_model_count": catalog_size,
        "baseline_model": _BASELINE_IMAGE_MODEL,
    }


def refresh_image_model_catalog() -> dict[str, Any]:
    """Force a zero-cost catalog resolution and return the resulting status."""
    resolve_image_model(allow_network=True, force_refresh=True)
    return image_model_resolution_status()


def image_model_capabilities(model: str | None = None) -> dict[str, bool]:
    """Return the supported hosted-tool parameters for a GPT Image model.

    Capabilities are derived from the parsed GPT Image release version rather
    than a static exact-name allowlist, so a new normal release is accepted
    without a source change.  Non-GPT-Image identifiers still fail closed
    before any request is sent.
    """
    resolved = str(model if model is not None else image_model()).strip().lower()
    match = _parse_gpt_image_id(resolved)
    if match is None:
        raise OpenAIImageFallbackError(f"openai_image_fallback_unsupported_image_model:{resolved or 'missing'}")
    version = tuple(int(part) for part in match.group("version").split("."))
    return {
        "action": True,
        "quality": True,
        "size": True,
        "output_format": True,
        # GPT Image 2 and later always process image inputs at high fidelity
        # and reject ``input_fidelity``; earlier GPT Image models support it.
        # This narrowly scoped exception is version-derived, not a per-model
        # mapping entry.
        "input_fidelity": version < (2,),
    }


def _tool_config(has_inputs: bool, model: str) -> dict[str, Any]:
    capabilities = image_model_capabilities(model)
    tool: dict[str, Any] = {"type": "image_generation", "model": model}
    if capabilities.get("action"):
        tool["action"] = "edit" if has_inputs else "generate"
    if capabilities.get("quality"):
        quality = os.environ.get("OPENAI_IMAGE_FALLBACK_QUALITY", "high").strip().lower() or "high"
        if quality not in {"low", "medium", "high", "auto"}:
            quality = "high"
        tool["quality"] = quality
    if capabilities.get("size"):
        size = os.environ.get("OPENAI_IMAGE_FALLBACK_SIZE", "auto").strip().lower() or "auto"
        if size not in {"1024x1024", "1024x1536", "1536x1024", "auto"}:
            size = "auto"
        tool["size"] = size
    if capabilities.get("output_format"):
        tool["output_format"] = "png"
    if has_inputs and capabilities.get("input_fidelity"):
        fidelity = os.environ.get("OPENAI_IMAGE_FALLBACK_INPUT_FIDELITY", "high").strip().lower() or "high"
        if fidelity not in {"low", "high"}:
            fidelity = "high"
        tool["input_fidelity"] = fidelity
    return tool


def _provider_error_detail(response: httpx.Response) -> str:
    """Return bounded non-secret provider detail for a failed API request."""
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        parts = [
            str(error.get("type") or "").strip(),
            str(error.get("code") or "").strip(),
            " ".join(str(error.get("message") or "").split()),
        ]
        detail = ":".join(part for part in parts if part)
    else:
        detail = " ".join(str(response.text or "").split())
    detail = detail.replace("\n", " ").strip()
    return detail[:240]


def generate_image(prompt: str, inputs: list[Path], request_timeout: int | None = None) -> tuple[bytes, str, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise OpenAIImageFallbackError("openai_image_fallback_not_configured")
    # This is the paid-spend boundary.  Normal production must never reach a
    # billable request merely because Codex quota is exhausted.
    if not paid_fallback_authorized():
        raise OpenAIImageFallbackError("openai_image_fallback_not_authorized")
    # Resolve the current compatible image model only here, at the spend
    # boundary.  Discovery failure degrades to cache/last-known/baseline and
    # never blocks the explicitly authorized generation.
    resolution = resolve_image_model(allow_network=True)
    content: list[dict[str, Any]] = [{"type": "input_text", "text": str(prompt)}]
    content.extend({"type": "input_image", "image_url": _data_url(path), "detail": "high"} for path in inputs)
    payload = {
        "model": responses_model(),
        "input": [{"role": "user", "content": content}],
        "tools": [_tool_config(bool(inputs), resolution.model)],
        # Responses API hosted tools use the string form for a required call.
        # The object form is reserved for function/MCP/custom tool choices and
        # produces HTTP 400 for image_generation.
        "tool_choice": "required",
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
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
        detail = _provider_error_detail(response)
        suffix = f":{detail}" if detail else ""
        raise OpenAIImageFallbackError(f"openai_image_fallback_http_{response.status_code}{suffix}")
    try:
        body = response.json()
    except ValueError as exc:
        raise OpenAIImageFallbackError("openai_image_fallback_invalid_json") from exc
    outputs = body.get("output") if isinstance(body, dict) else None
    if not isinstance(outputs, list):
        raise OpenAIImageFallbackError("openai_image_fallback_output_missing")
    encoded_results = [
        item.get("result") for item in outputs
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
    return data, _mime_for_bytes(data), resolution.model

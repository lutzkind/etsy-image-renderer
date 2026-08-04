from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import queue
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.responses import Response

APP_VERSION = "1.4.0"
CONTRACT_VERSION = "luxlm-render-contract-v2"
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 25 * 1024 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

MODE_CONTRACTS: dict[str, dict[str, Any]] = {
    "minimal_frame": {"expected_input_count": 1, "output_kind": "final_asset", "generated_text": False},
    "lifestyle": {"expected_input_count": 2, "output_kind": "final_asset", "generated_text": False},
    "orientation": {"expected_input_count": 1, "output_kind": "final_asset", "generated_text": False},
    "before_after_card": {"expected_input_count": 2, "output_kind": "decorative_asset", "generated_text": False},
    "information_card": {"expected_input_count": 2, "output_kind": "decorative_asset", "generated_text": False},
    "decorative_asset": {"expected_input_count": None, "output_kind": "decorative_asset", "generated_text": False},
    "designed_card": {"expected_input_count": None, "output_kind": "final_asset", "generated_text": True},
}
ALLOWED_MODES = set(MODE_CONTRACTS)
EXPECTED_INPUTS = {key: value["expected_input_count"] for key, value in MODE_CONTRACTS.items() if value["expected_input_count"] is not None}
STRICT_NO_TEXT = [
    "text", "letters", "numbers", "signature", "logo", "watermark", "blank_caption_sheet",
    "paper_mat", "marketing_panel", "empty_label_region",
]
DESIGNED_CARD_PROHIBITIONS = {"signature", "logo", "watermark", "competitor_branding", "price", "invented_claims"}

_RENDER_LOCK = threading.BoundedSemaphore(1)
_ASYNC_JOBS: dict[str, dict[str, Any]] = {}
_ASYNC_JOBS_LOCK = threading.Lock()
_REQUEST_DIGESTS: dict[str, dict[str, Any]] = {}
_REQUEST_DIGESTS_LOCK = threading.Lock()
ASYNC_JOB_TTL_SECONDS = 3600
REQUEST_DIGEST_TTL_SECONDS = 3600
_ASYNC_QUEUE: queue.Queue[str] = queue.Queue()
_ASYNC_QUEUE_IDS: set[str] = set()
_ASYNC_WORKER_LOCK = threading.Lock()
_ASYNC_WORKER_STARTED = False
_ASYNC_STATE_RESTORED = False
app = FastAPI(title="Etsy Codex Renderer", version=APP_VERSION)


class AssetRole(BaseModel):
    role: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=4000)
    preservation: str = Field(default="reference_only", max_length=120)
    exact_pixel_preservation: bool = False
    transform_allowed: bool = True

    @field_validator("role", "preservation")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return " ".join(str(value).strip().split())


class RenderRequest(BaseModel):
    mode: str
    input_urls: list[str] = Field(default_factory=list)
    context: str = Field(default="", max_length=2000)
    module: str = Field(default="", max_length=120)
    template_family: str = Field(default="", max_length=160)
    template_reference_url: str = Field(default="", max_length=4000)
    layout_contract: dict[str, Any] = Field(default_factory=dict)
    listing_assets: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    decorative_asset_requests: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    card_brief: dict[str, Any] = Field(default_factory=dict)
    asset_roles: list[AssetRole] = Field(default_factory=list, max_length=8)
    generation_instructions: dict[str, Any] = Field(default_factory=dict)
    prohibited_elements: list[str] = Field(default_factory=list, max_length=40)
    expected_input_count: int | None = Field(default=None, ge=0, le=8)
    prompt_version: str = Field(default="luxlm-decorative-asset-v1", max_length=120)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in ALLOWED_MODES:
            raise ValueError("invalid_render_mode")
        return normalized

    @field_validator("input_urls")
    @classmethod
    def validate_urls_shape(cls, value: list[str]) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("invalid_input_urls")
        return [str(item).strip() for item in value]

    @field_validator("prohibited_elements")
    @classmethod
    def normalize_prohibited(cls, value: list[str]) -> list[str]:
        return sorted({" ".join(str(item).strip().lower().split()) for item in value if str(item).strip()})

    @model_validator(mode="after")
    def validate_contract(self) -> "RenderRequest":
        contract = MODE_CONTRACTS[self.mode]
        expected = self.expected_input_count

        if self.mode == "designed_card":
            if not self.asset_roles or not 1 <= len(self.asset_roles) <= 8:
                raise ValueError("designed_card_requires_asset_roles")
            listing_count = len(self.listing_assets)
            if not 1 <= listing_count <= 8:
                raise ValueError("designed_card_requires_listing_assets")
            if expected is None or expected != listing_count:
                raise ValueError("designed_card_expected_input_count_mismatch")
            if len(self.input_urls) != listing_count:
                raise ValueError("designed_card_input_count_mismatch")
            role_urls = [role.url.strip() for role in self.asset_roles]
            if self.input_urls != role_urls:
                raise ValueError("asset_roles_must_match_input_urls")
            self.input_urls = role_urls
            if not self.module.strip():
                raise ValueError("designed_card_requires_module")
            if not self.template_family.strip():
                raise ValueError("designed_card_requires_template_family")
            headline = self.card_brief.get("headline") if isinstance(self.card_brief, dict) else None
            if not isinstance(headline, str) or not headline.strip():
                raise ValueError("designed_card_requires_headline")
            if not DESIGNED_CARD_PROHIBITIONS.issubset(set(self.prohibited_elements)):
                raise ValueError("designed_card_requires_prohibitions")
        else:
            if self.asset_roles:
                role_urls = [role.url.strip() for role in self.asset_roles]
                if self.input_urls and self.input_urls != role_urls:
                    raise ValueError("asset_roles_must_match_input_urls")
                self.input_urls = role_urls

        if self.mode == "decorative_asset":
            if expected is None:
                expected = len(self.input_urls)
            if expected < 1 or expected > 8:
                raise ValueError("decorative_asset_requires_exact_input_count")
            if not set(STRICT_NO_TEXT).issubset(set(self.prohibited_elements)):
                raise ValueError("decorative_asset_requires_strict_no_text_prohibitions")
            if not self.asset_roles:
                raise ValueError("decorative_asset_requires_asset_roles")
        if expected is None:
            expected = contract["expected_input_count"]
        if expected is not None and len(self.input_urls) != expected:
            raise ValueError("invalid_input_count")
        if self.asset_roles and len({role.role for role in self.asset_roles}) != len(self.asset_roles):
            raise ValueError("duplicate_asset_role")
        if self.template_reference_url and not self.template_reference_url.startswith("https://"):
            raise ValueError("invalid_template_reference_url")
        return self




def _token() -> str:
    return os.environ.get("ETSY_CODEX_RENDERER_TOKEN", "").strip()


def _require_auth(authorization: str | None, x_renderer_token: str | None) -> None:
    expected = _token()
    supplied = (authorization or "").strip()
    if supplied.lower().startswith("bearer "):
        supplied = supplied[7:].strip()
    supplied = supplied or (x_renderer_token or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="renderer_not_configured")
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="unauthorized")


def _public_addresses(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("input_host_unresolvable") from exc
    addresses = sorted({str(info[4][0]).split("%", 1)[0] for info in infos})
    if not addresses:
        raise ValueError("input_host_unresolvable")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_reserved or address.is_unspecified:
            raise ValueError("input_url_not_public")
    return addresses


def _validate_public_https_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("invalid_input_url")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise ValueError("input_url_not_public")
    _public_addresses(hostname)
    return url


def _sniff_image(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise ValueError("unsupported_image")


def _download_image(url: str, target_stem: Path) -> Path:
    current = _validate_public_https_url(url)
    with httpx.Client(timeout=60, follow_redirects=False, headers={"User-Agent": "Etsy-Codex-Renderer/1.3", "Accept": "image/png,image/jpeg,image/webp"}) as client:
        for _ in range(4):
            response = client.get(current)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location", "")
                if not location:
                    raise ValueError("invalid_input_redirect")
                current = _validate_public_https_url(urljoin(current, location))
                continue
            response.raise_for_status()
            data = response.content
            if len(data) > MAX_INPUT_BYTES:
                raise ValueError("input_image_too_large")
            _, suffix = _sniff_image(data)
            path = target_stem.with_suffix(suffix)
            path.write_bytes(data)
            return path
    raise ValueError("too_many_input_redirects")


def _role_contract(request: RenderRequest) -> dict[str, Any]:
    contract = MODE_CONTRACTS[request.mode]
    return {
        "contract_version": CONTRACT_VERSION,
        "mode": request.mode,
        "module": request.module,
        "template_family": request.template_family,
        "template_reference_url": request.template_reference_url,
        "layout_contract": request.layout_contract,
        "listing_assets": request.listing_assets,
        "card_brief": request.card_brief,
        "generation_instructions": request.generation_instructions,
        "expected_input_count": request.expected_input_count if request.expected_input_count is not None else contract["expected_input_count"],
        "asset_roles": [role.model_dump(mode="json") for role in request.asset_roles],
        "output_kind": contract["output_kind"],
        "generated_text": contract["generated_text"],
        "prohibited_elements": sorted(set((STRICT_NO_TEXT if request.mode == "decorative_asset" else []) + request.prohibited_elements)),
        "layout_contract_hash": hashlib.sha256(json.dumps(request.layout_contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }


def _prompt(request: RenderRequest | str, context: str = "") -> str:
    if isinstance(request, str):
        legacy_mode = request
        count = EXPECTED_INPUTS[legacy_mode]
        legacy_roles = [AssetRole(role=f"input_{index}", url="https://example.com/input.jpg") for index in range(count)]
        request = RenderRequest(
            mode=legacy_mode,
            input_urls=[role.url for role in legacy_roles],
            asset_roles=legacy_roles if legacy_mode == "decorative_asset" else [],
            prohibited_elements=STRICT_NO_TEXT if legacy_mode == "decorative_asset" else [],
            context=context,
        )
    role_contract = _role_contract(request)
    role_lines = []
    for index, role in enumerate(request.asset_roles, 1):
        preservation = "EXACT PIXEL PRESERVATION REQUIRED" if role.exact_pixel_preservation else role.preservation
        transform = "no transform" if not role.transform_allowed else "transform only as contract permits"
        role_lines.append(f"Asset {index}: role={role.role}; preservation={preservation}; {transform}.")
    if request.mode == "designed_card":
        common = (
            "Use the built-in image_gen/image_generation tool exactly once. Do not call an external image API, do not run image_gen.py, "
            "and do not create SVG, HTML, CSS, placeholder art, or a programmatic drawing. Generate exactly one complete premium Etsy gallery card. "
            "Preserve supplied people, identities, artwork, and exact-pixel assets. Use exactly the approved card_brief headline, body, and bullets, "
            "and no other text; never invent, paraphrase, or add copy. Adapt composition, palette, and visual language to the listing theme. "
            "DESIGN REFERENCE ONLY: the separately supplied template is inspiration-only. Use it for inspiration only and never copy it exactly. "
            "Reject generic dashboards, presentation slides, ivory-panel boilerplate, generic information panels, icons, badges, clipart, and Canva-like layouts. "
            "Do not include signatures, logos, watermarks, competitor branding, prices, or invented claims. "
            "Copy the exact generated raster to ./rendered-output.png without redrawing or re-encoding it."
        )
        instruction = "Create one cohesive, premium editorial Etsy gallery card for the supplied listing and module."
    else:
        common = (
            "Use the built-in image_gen/image_generation tool exactly once. Do not call an external image API, do not run image_gen.py, and do not create SVG, HTML, CSS, placeholder art, or a programmatic drawing. Generate exactly one polished raster image, then copy the exact generated raster to ./rendered-output.png without redrawing or re-encoding it. This is a decorative visual asset, not the final typography compositor. Never generate words, letters, numbers, pseudo-lettering, signatures, logos, watermarks, blank caption sheets, paper mats, empty label regions, marketing panels, generic information panels, prices, badges, or invented claims. Do not copy competitor branding, exact coordinates, distinctive protected elements, or source-image text. Preserve any role marked exact pixel preservation; the final system may composite that raster deterministically afterward."
        )
        instruction = {
            "minimal_frame": "Create a restrained ecommerce frame presentation around the exact finished artwork.",
            "lifestyle": "Use the room as the physical context and the second asset as the exact artwork target; preserve the room and do not redraw the artwork.",
            "orientation": "Create a clean presentation of the exact complete artwork without cropping important architecture or subject content.",
            "before_after_card": "Create a visual-only before/after supporting asset; do not add text or a generic information panel.",
            "information_card": "Create a visual-only supporting asset; do not add text or a generic information panel. Final composition and wording are deterministic outside this service.",
            "decorative_asset": f"Create only the requested decorative visual treatment for module {request.module or 'unspecified'}; obey the supplied layout and asset roles.",
        }[request.mode]
    compact_context = " ".join((request.context or "").split())[:700]
    return "\n\n".join([
        common,
        instruction,
        "STRUCTURED INPUT CONTRACT:\n" + json.dumps(role_contract, ensure_ascii=True, sort_keys=True),
        "ROLE INSTRUCTIONS:\n" + ("\n".join(role_lines) or "No role lines supplied."),
        "GENERATION INSTRUCTIONS:\n" + json.dumps(request.generation_instructions, ensure_ascii=True, sort_keys=True),
        f"Listing-specific context: {compact_context}",
        "Return only a brief confirmation after generating the image.",
    ])




def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "/root/.codex"))


def _snapshot(workspace: Path) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for root in (_codex_home() / "generated_images", workspace):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                stat = path.stat()
                result[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
    return result


def _new_outputs(workspace: Path, before: dict[str, tuple[int, int]], inputs: list[Path]) -> list[Path]:
    excluded = {str(path.resolve()) for path in inputs}
    outputs: list[Path] = []
    for root in (_codex_home() / "generated_images", workspace):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            resolved = str(path.resolve())
            if resolved in excluded:
                continue
            stat = path.stat()
            if before.get(resolved) != (stat.st_mtime_ns, stat.st_size):
                outputs.append(path)
    outputs.sort(key=lambda item: item.stat().st_mtime_ns)
    return outputs


def _codex_command(workspace: Path, inputs: list[Path]) -> list[str]:
    command = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "danger-full-access", "--ephemeral", "--enable", "image_generation", "-C", str(workspace), "--json"]
    for path in inputs:
        command.extend(["-i", str(path)])
    command.append("-")
    return command


def readiness() -> dict[str, Any]:
    binary = shutil.which("codex")
    version = ""
    authenticated = False
    image_generation = False
    if binary:
        try:
            version_result = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=8)
            version = (version_result.stdout or version_result.stderr).strip()
            login = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=8)
            authenticated = login.returncode == 0 and "not logged in" not in ((login.stdout or "") + (login.stderr or "")).lower()
            features = subprocess.run(["codex", "features", "list"], capture_output=True, text=True, timeout=8)
            for line in (features.stdout or "").splitlines():
                parts = line.split()
                if parts and parts[0] == "image_generation":
                    image_generation = parts[-1].lower() == "true"
                    break
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "ready": bool(binary and authenticated and image_generation and _token()),
        "binary": bool(binary), "version": version, "authenticated": authenticated,
        "image_generation": image_generation, "token_configured": bool(_token()),
        "renderer": "codex-local", "app_version": APP_VERSION, "contract_version": CONTRACT_VERSION,
    }


def _request_hash(request: RenderRequest) -> str:
    payload = request.model_dump(mode="json")
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _claim_request(request_hash: str) -> None:
    now = time.time()
    with _REQUEST_DIGESTS_LOCK:
        for digest, row in list(_REQUEST_DIGESTS.items()):
            if now - float(row.get("created_at") or 0) > REQUEST_DIGEST_TTL_SECONDS:
                _REQUEST_DIGESTS.pop(digest, None)
        if request_hash in _REQUEST_DIGESTS:
            raise ValueError("duplicate_request_hash")
        _REQUEST_DIGESTS[request_hash] = {"created_at": now, "status": "running"}


def _release_failed_request(request_hash: str) -> None:
    with _REQUEST_DIGESTS_LOCK:
        _REQUEST_DIGESTS.pop(request_hash, None)

def _render(request: RenderRequest) -> tuple[bytes, str, str]:
    urls = [_validate_public_https_url(url) for url in request.input_urls]
    template_url = _validate_public_https_url(request.template_reference_url) if request.template_reference_url else None
    request_hash = _request_hash(request)
    async_context = bool(getattr(_ASYNC_RENDER_CONTEXT, "active", False))
    if not async_context:
        _claim_request(request_hash)
    if not readiness()["ready"]:
        if not async_context:
            _release_failed_request(request_hash)
        raise RuntimeError("renderer_not_ready")
    if async_context:
        _RENDER_LOCK.acquire()
        acquired = True
    else:
        acquired = _RENDER_LOCK.acquire(timeout=5)
    if not acquired:
        if not async_context:
            _release_failed_request(request_hash)
        raise RuntimeError("renderer_busy")
    outputs: list[Path] = []
    try:
        root = Path(os.environ.get("RENDER_DATA_DIR", "/data"))
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="render-", dir=root) as temp:
            workspace = Path(temp)
            inputs = [_download_image(url, workspace / f"input-{index}") for index, url in enumerate(urls, 1)]
            reference = None
            if template_url:
                reference = _download_image(template_url, workspace / "design-reference")
            command_inputs = list(inputs)
            if reference is not None:
                command_inputs.append(reference)
            before = _snapshot(workspace)
            timeout = max(60, min(int(os.environ.get("CODEX_RENDER_TIMEOUT_SECONDS", "900")), 1800))
            prompt_context = ""
            if reference is not None:
                prompt_context = "The final supplied image is DESIGN REFERENCE ONLY and is inspiration-only. Do not treat it as a listing asset, do not preserve its pixels, and do not copy it exactly."
            result = subprocess.run(_codex_command(workspace, command_inputs), cwd=workspace, input=_prompt(request, prompt_context), text=True, capture_output=True, timeout=timeout, check=False)
            outputs = _new_outputs(workspace, before, command_inputs)
            if result.returncode != 0:
                combined = ((result.stderr or "") + "\n" + (result.stdout or "")).lower()
                if any(term in combined for term in ("usage limit", "quota", "too many requests", "429")):
                    raise RuntimeError("codex_quota_unavailable")
                if any(term in combined for term in ("not logged in", "unauthorized", "401")):
                    raise RuntimeError("codex_authentication_failed")
                raise RuntimeError("codex_render_failed")
            unique: dict[str, tuple[bytes, str]] = {}
            for path in outputs:
                data = path.read_bytes()
                if len(data) > MAX_OUTPUT_BYTES:
                    raise RuntimeError("output_too_large")
                mime, _ = _sniff_image(data)
                unique[hashlib.sha256(data).hexdigest()] = (data, mime)
            if not unique:
                raise RuntimeError("output_missing")
            if len(unique) != 1:
                raise RuntimeError("output_ambiguous")
            digest, (data, mime) = next(iter(unique.items()))
            with _REQUEST_DIGESTS_LOCK:
                if request_hash in _REQUEST_DIGESTS:
                    _REQUEST_DIGESTS[request_hash].update({"status": "succeeded", "output_sha256": digest})
            return data, mime, digest
    except Exception:
        if not async_context:
            _release_failed_request(request_hash)
        raise
    finally:
        generated_root = (_codex_home() / "generated_images").resolve()
        for path in outputs:
            try:
                if generated_root in path.resolve().parents:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        _RENDER_LOCK.release()


_ASYNC_HASH_INDEX = globals().setdefault("_ASYNC_HASH_INDEX", {})
_ASYNC_RENDER_CONTEXT = globals().setdefault("_ASYNC_RENDER_CONTEXT", threading.local())


def _async_job_root() -> Path:
    root = Path(os.environ.get("RENDER_DATA_DIR", "/data")) / "async-jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _async_job_meta_path(job_id: str) -> Path:
    return _async_job_root() / f"{job_id}.json"


def _async_job_result_path(job_id: str) -> Path:
    return _async_job_root() / f"{job_id}.result"


def _persist_async_job(job_id: str, job: dict[str, Any]) -> None:
    payload = {key: value for key, value in job.items() if key != "data"}
    target = _async_job_meta_path(job_id)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    os.replace(temporary, target)


def _persist_async_result(job_id: str, data: bytes) -> str:
    target = _async_job_result_path(job_id)
    temporary = target.with_suffix(".result.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, target)
    return str(target)


def _load_async_job(job_id: str) -> dict[str, Any]:
    with _ASYNC_JOBS_LOCK:
        current = _ASYNC_JOBS.get(str(job_id))
        if current:
            return dict(current)
    path = _async_job_meta_path(str(job_id))
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    with _ASYNC_JOBS_LOCK:
        _ASYNC_JOBS[str(job_id)] = dict(payload)
    return dict(payload)


def _load_async_result(job_id: str, job: dict[str, Any]) -> bytes:
    data = job.get("data")
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    path = Path(str(job.get("result_path") or _async_job_result_path(job_id)))
    if not path.is_file():
        raise FileNotFoundError("render_result_missing")
    return path.read_bytes()


def _enqueue_async_job(job_id: str) -> None:
    global _ASYNC_WORKER_STARTED
    with _ASYNC_JOBS_LOCK:
        if job_id in _ASYNC_QUEUE_IDS:
            return
        _ASYNC_QUEUE_IDS.add(job_id)
    with _ASYNC_WORKER_LOCK:
        if not _ASYNC_WORKER_STARTED:
            threading.Thread(target=_async_worker_loop, daemon=True, name="etsy-codex-render-worker").start()
            _ASYNC_WORKER_STARTED = True
    _ASYNC_QUEUE.put(job_id)


def _async_worker_loop() -> None:
    while True:
        job_id = _ASYNC_QUEUE.get()
        with _ASYNC_JOBS_LOCK:
            _ASYNC_QUEUE_IDS.discard(job_id)
        try:
            job = _load_async_job(job_id)
            if str(job.get("status") or "") not in {"queued", "running"}:
                continue
            request_payload = job.get("request")
            if not isinstance(request_payload, dict):
                with _ASYNC_JOBS_LOCK:
                    if job_id in _ASYNC_JOBS:
                        _ASYNC_JOBS[job_id].update({"status": "failed", "error": "render_request_missing"})
                        _persist_async_job(job_id, _ASYNC_JOBS[job_id])
                continue
            _run_async_job(job_id, RenderRequest.model_validate(request_payload))
        finally:
            _ASYNC_QUEUE.task_done()


def _restore_async_state() -> None:
    global _ASYNC_STATE_RESTORED
    with _ASYNC_WORKER_LOCK:
        if _ASYNC_STATE_RESTORED:
            return
        _ASYNC_STATE_RESTORED = True
    for path in _async_job_root().glob("*.json"):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        job_id = path.stem
        status = str(payload.get("status") or "queued")
        if status == "running":
            payload["status"] = "queued"
            payload["recovered_after_restart"] = True
        request_hash = str(payload.get("request_hash") or "")
        with _ASYNC_JOBS_LOCK:
            _ASYNC_JOBS[job_id] = payload
            if request_hash and status in {"queued", "running", "succeeded"}:
                _ASYNC_HASH_INDEX[request_hash] = job_id
        _persist_async_job(job_id, payload)
        if str(payload.get("status") or "") == "queued":
            _enqueue_async_job(job_id)


def _prune_async_jobs() -> None:
    _restore_async_state()
    cutoff = time.time() - ASYNC_JOB_TTL_SECONDS
    with _ASYNC_JOBS_LOCK:
        for job_id, job in list(_ASYNC_JOBS.items()):
            created_at = job.get("created_at")
            valid_created_at = False
            try:
                if isinstance(created_at, bool):
                    raise ValueError
                created_at = float(created_at)
                valid_created_at = created_at > 0
            except (TypeError, ValueError, OverflowError):
                pass
            if valid_created_at and created_at < cutoff and job.get("status") in {"succeeded", "failed"}:
                _ASYNC_JOBS.pop(job_id, None)
                request_hash = str(job.get("request_hash") or "")
                if request_hash and _ASYNC_HASH_INDEX.get(request_hash) == job_id:
                    _ASYNC_HASH_INDEX.pop(request_hash, None)
                try:
                    _async_job_meta_path(job_id).unlink(missing_ok=True)
                    _async_job_result_path(job_id).unlink(missing_ok=True)
                except OSError:
                    pass
        for request_hash, job_id in list(_ASYNC_HASH_INDEX.items()):
            if job_id not in _ASYNC_JOBS:
                _ASYNC_HASH_INDEX.pop(request_hash, None)




def _run_async_job(job_id: str, request: RenderRequest) -> None:
    with _ASYNC_JOBS_LOCK:
        job = _ASYNC_JOBS.get(job_id)
        if not job:
            return
        job["status"] = "running"
        _persist_async_job(job_id, job)
    _ASYNC_RENDER_CONTEXT.active = True
    try:
        data, mime, digest = _render(request)
        with _ASYNC_JOBS_LOCK:
            if job_id in _ASYNC_JOBS:
                result_path = _persist_async_result(job_id, data)
                _ASYNC_JOBS[job_id].update({
                    "status": "succeeded",
                    "mime": mime,
                    "digest": digest,
                    "output_sha256": digest,
                    "result_path": result_path,
                    "completed_at": time.time(),
                })
                _persist_async_job(job_id, _ASYNC_JOBS[job_id])
    except Exception as exc:
        request_hash = _request_hash(request)
        with _ASYNC_JOBS_LOCK:
            if job_id in _ASYNC_JOBS:
                _ASYNC_JOBS[job_id].update({
                    "status": "failed",
                    "error": str(exc)[:200],
                    "completed_at": time.time(),
                })
                _persist_async_job(job_id, _ASYNC_JOBS[job_id])
            if _ASYNC_HASH_INDEX.get(request_hash) == job_id:
                _ASYNC_HASH_INDEX.pop(request_hash, None)
        _release_failed_request(request_hash)
    finally:
        _ASYNC_RENDER_CONTEXT.active = False


def _async_job_response(job_id: str, job: dict[str, Any]) -> JSONResponse:
    status = str(job.get("status") or "queued")
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "request_hash": str(job.get("request_hash") or ""),
        "renderer": "codex-local",
        "app_version": APP_VERSION,
        "contract_version": CONTRACT_VERSION,
    }
    if status == "queued" or status == "running":
        return JSONResponse(payload, status_code=202)
    if status == "failed":
        payload["error"] = str(job.get("error") or "codex_render_failed")
        return JSONResponse(payload, status_code=200)
    payload.update({
        "result_url": f"/render-async/{job_id}/result",
        "mime": str(job.get("mime") or "image/png"),
        "output_sha256": str(job.get("output_sha256") or job.get("digest") or ""),
    })
    return JSONResponse(payload, status_code=200)


@app.get("/health")
def health() -> dict[str, Any]:
    _restore_async_state()
    payload = readiness()
    with _ASYNC_JOBS_LOCK:
        payload["queued_jobs"] = sum(1 for job in _ASYNC_JOBS.values() if job.get("status") == "queued")
        payload["running_jobs"] = sum(1 for job in _ASYNC_JOBS.values() if job.get("status") == "running")
    payload["persistent_queue"] = True
    return payload


@app.post("/render")
async def render(request: RenderRequest, authorization: str | None = Header(default=None), x_renderer_token: str | None = Header(default=None)) -> Response:
    _require_auth(authorization, x_renderer_token)
    try:
        data, mime, digest = await asyncio.to_thread(_render, request)
    except ValueError as exc:
        code = str(exc)
        raise HTTPException(status_code=409 if code == "duplicate_request_hash" else 400, detail=code) from exc
    except RuntimeError as exc:
        code = str(exc)
        raise HTTPException(status_code=429 if code == "renderer_busy" else 503, detail=code) from exc
    return Response(content=data, media_type=mime, headers={
        "Cache-Control": "no-store", "X-Renderer": "codex-local", "X-Renderer-Version": APP_VERSION,
        "X-Render-Mode": request.mode, "X-Image-Sha256": digest, "X-Render-Request-Sha256": _request_hash(request),
    })


@app.post("/render-async", status_code=202)
def render_async(request: RenderRequest, authorization: str | None = Header(default=None), x_renderer_token: str | None = Header(default=None)) -> JSONResponse:
    _require_auth(authorization, x_renderer_token)
    try:
        for url in request.input_urls:
            _validate_public_https_url(url)
        if request.template_reference_url:
            _validate_public_https_url(request.template_reference_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not readiness()["ready"]:
        raise HTTPException(status_code=503, detail="renderer_not_ready")
    _restore_async_state()
    _prune_async_jobs()
    request_hash = _request_hash(request)
    with _ASYNC_JOBS_LOCK:
        existing_id = _ASYNC_HASH_INDEX.get(request_hash)
        if existing_id:
            existing = _load_async_job(existing_id)
            if existing and existing.get("status") in {"queued", "running", "succeeded"}:
                return _async_job_response(existing_id, existing)
            _ASYNC_HASH_INDEX.pop(request_hash, None)
        job_id = uuid.uuid4().hex
        _ASYNC_HASH_INDEX[request_hash] = job_id
        _ASYNC_JOBS[job_id] = {
            "status": "queued",
            "created_at": time.time(),
            "mode": request.mode,
            "request_hash": request_hash,
            "request": request.model_dump(mode="json"),
        }
        _persist_async_job(job_id, _ASYNC_JOBS[job_id])
    _enqueue_async_job(job_id)
    return JSONResponse({"job_id": job_id, "status": "queued", "request_hash": request_hash, "contract_version": CONTRACT_VERSION}, status_code=202)


@app.get("/render-async/{job_id}")
def render_async_status(job_id: str, authorization: str | None = Header(default=None), x_renderer_token: str | None = Header(default=None)) -> JSONResponse:
    _require_auth(authorization, x_renderer_token)
    _prune_async_jobs()
    job = _load_async_job(str(job_id))
    if not job:
        raise HTTPException(status_code=404, detail="render_job_not_found")
    return _async_job_response(str(job_id), job)


@app.get("/render-async/{job_id}/result")
def render_async_result(job_id: str, authorization: str | None = Header(default=None), x_renderer_token: str | None = Header(default=None)) -> Response:
    _require_auth(authorization, x_renderer_token)
    _prune_async_jobs()
    job = _load_async_job(str(job_id))
    if not job:
        raise HTTPException(status_code=404, detail="render_job_not_found")
    status = str(job.get("status") or "queued")
    if status in {"queued", "running"}:
        raise HTTPException(status_code=202, detail="render_job_not_ready")
    if status == "failed":
        raise HTTPException(status_code=409, detail=str(job.get("error") or "codex_render_failed"))
    try:
        data = _load_async_result(str(job_id), job)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=410, detail="render_result_missing") from exc
    return Response(content=data, media_type=str(job.get("mime") or "image/png"), headers={
        "Cache-Control": "no-store", "X-Renderer": "codex-local", "X-Renderer-Version": APP_VERSION,
        "X-Image-Sha256": str(job.get("output_sha256") or job.get("digest") or ""),
        "X-Render-Request-Sha256": str(job.get("request_hash") or ""),
    })

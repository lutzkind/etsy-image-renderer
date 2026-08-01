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
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "1.1.1"
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 25 * 1024 * 1024
ALLOWED_MODES = {
    "minimal_frame",
    "lifestyle",
    "orientation",
    "before_after_card",
    "information_card",
}
EXPECTED_INPUTS = {
    "minimal_frame": 1,
    "lifestyle": 2,
    "orientation": 1,
    "before_after_card": 2,
    "information_card": 2,
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_RENDER_LOCK = threading.BoundedSemaphore(1)
_ASYNC_JOBS: dict[str, dict] = {}
_ASYNC_JOBS_LOCK = threading.Lock()
ASYNC_JOB_TTL_SECONDS = 3600
app = FastAPI(title="Etsy Codex Renderer", version=APP_VERSION)


class RenderRequest(BaseModel):
    mode: str
    input_urls: list[str]
    context: str = Field(default="", max_length=2000)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in ALLOWED_MODES:
            raise ValueError("invalid_render_mode")
        return normalized

    @field_validator("input_urls")
    @classmethod
    def validate_urls_shape(cls, value: list[str]) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("invalid_input_urls")
        return value


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
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError("input_url_not_public")
    return addresses


def _validate_public_https_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
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
    with httpx.Client(timeout=60, follow_redirects=False, headers={
        "User-Agent": "Etsy-Codex-Renderer/1.0",
        "Accept": "image/png,image/jpeg,image/webp",
    }) as client:
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


def _prompt(mode: str, context: str) -> str:
    common = (
        "Use the built-in image_gen/image_generation tool exactly once. "
        "Do not call an external image API, do not run image_gen.py, and do not create SVG, HTML, CSS, "
        "placeholder art, or a programmatic drawing. Generate exactly one polished raster image. "
        "After generation succeeds, use only a simple filesystem copy to place the exact generated raster "
        "file at ./rendered-output.png in the current workspace. Do not redraw, convert, or re-encode it. "
        "Do not finish until ./rendered-output.png exists. "
        "Do not add captions, logos, watermarks, signatures, prices, badges, or marketing text."
    )
    instructions = {
        "minimal_frame": (
            "Image 1 is the exact finished artwork. Create a premium minimal-frame Etsy hero mockup. "
            "Use one simple neutral frame and a clean restrained background. The artwork must occupy about "
            "75 to 85 percent of the canvas so it remains readable as a small listing thumbnail. Preserve the "
            "artwork's subjects, colours, linework, texture, proportions, composition, and intentional text. "
            "Do not invent a different artwork and do not add additional frames."
        ),
        "lifestyle": (
            "Image 1 is the original interior photograph. Image 2 is the exact finished artwork. Place Image 2 "
            "naturally in the clearest existing framed-art or wall-display area. Preserve the room, furniture, "
            "camera angle, lighting, frame edges, mat, shadows, and reflections. Preserve the artwork exactly; "
            "do not redesign the room or artwork and do not add extra frames."
        ),
        "orientation": (
            "Image 1 is the exact finished artwork. Create one clean ecommerce presentation that clearly shows "
            "its full portrait or landscape composition at a large readable size. Use a neutral studio background "
            "and one restrained print or frame presentation. Do not crop important outer details."
        ),
        "before_after_card": (
            "Image 1 is the exact source photograph for this listing. Image 2 is the exact finished listing artwork "
            "created from the same subject and is the primary visual/style anchor. Create one polished, listing-specific "
            "before-and-after ecommerce composition: show Image 1 as the before/source view and create the after view "
            "as a faithful visual transformation of Image 1 using Image 2's medium, palette, texture, and line treatment. "
            "Image 2 is a style anchor only and must not be reproduced as a separate panel or copied as content. Preserve "
            "the source subject; do not invent a different subject. Never copy Image 2's words, names, dates, signatures, "
            "logos, watermarks, caption area, paper/mat edge, or border. Do not add extra panels, frames, captions, "
            "labels, or marketing text."
        ),
        "information_card": (
            "Image 1 is a relevant source, mockup, or supporting context image for this exact listing. Image 2 is the "
            "exact finished listing artwork and the primary visual/style anchor. Create one polished, listing-specific "
            "visual information composition using Image 1 as the topic/context and Image 2 only for the artwork's "
            "medium, palette, texture, and line treatment. Do not reproduce Image 2 as a separate panel or copy its "
            "writing, names, dates, signatures, logos, watermarks, caption area, paper/mat edge, or border. Keep the "
            "listing subject recognizable and do not invent product claims or add captions, labels, logos, signatures, "
            "watermarks, badges, prices, or marketing text; exact approved wording will be overlaid deterministically later."
        ),
    }
    suffix = f" Product context: {' '.join(context.split())[:500]}." if context.strip() else ""
    return f"{common}\n\n{instructions[mode]}{suffix}\n\nReturn only a brief confirmation after generating the image."


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
    command = [
        "codex", "exec", "--skip-git-repo-check", "--sandbox", "danger-full-access",
        "--ephemeral", "--enable", "image_generation", "-C", str(workspace), "--json",
    ]
    for path in inputs:
        command.extend(["-i", str(path)])
    command.append("-")
    return command


def readiness() -> dict:
    binary = shutil.which("codex")
    version = ""
    authenticated = False
    image_generation = False
    if binary:
        try:
            version_result = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=8)
            version = (version_result.stdout or version_result.stderr).strip()
            login = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=8)
            authenticated = login.returncode == 0 and "not logged in" not in (
                (login.stdout or "") + (login.stderr or "")
            ).lower()
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
        "binary": bool(binary),
        "version": version,
        "authenticated": authenticated,
        "image_generation": image_generation,
        "token_configured": bool(_token()),
        "renderer": "codex-local",
        "app_version": APP_VERSION,
    }


def _render(request: RenderRequest) -> tuple[bytes, str, str]:
    expected = EXPECTED_INPUTS[request.mode]
    if len(request.input_urls) != expected:
        raise ValueError("invalid_input_count")
    urls = [_validate_public_https_url(url) for url in request.input_urls]
    if not readiness()["ready"]:
        raise RuntimeError("renderer_not_ready")
    if not _RENDER_LOCK.acquire(timeout=5):
        raise RuntimeError("renderer_busy")
    outputs: list[Path] = []
    try:
        root = Path(os.environ.get("RENDER_DATA_DIR", "/data"))
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="render-", dir=root) as temp:
            workspace = Path(temp)
            inputs = [_download_image(url, workspace / f"input-{index}") for index, url in enumerate(urls, 1)]
            before = _snapshot(workspace)
            timeout = max(60, min(int(os.environ.get("CODEX_RENDER_TIMEOUT_SECONDS", "900")), 1800))
            result = subprocess.run(
                _codex_command(workspace, inputs), cwd=workspace, input=_prompt(request.mode, request.context),
                text=True, capture_output=True, timeout=timeout, check=False,
            )
            outputs = _new_outputs(workspace, before, inputs)
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
                workspace_files = []
                for candidate in workspace.rglob("*"):
                    if candidate.is_file():
                        try:
                            workspace_files.append({
                                "path": str(candidate.relative_to(workspace)),
                                "bytes": candidate.stat().st_size,
                            })
                        except OSError:
                            pass
                print(json.dumps({
                    "event": "codex_render_output_missing",
                    "returncode": result.returncode,
                    "stdout_tail": (result.stdout or "")[-12000:],
                    "stderr_tail": (result.stderr or "")[-6000:],
                    "workspace_files": workspace_files[:50],
                }, ensure_ascii=True), flush=True)
                raise RuntimeError("output_missing")
            if len(unique) != 1:
                raise RuntimeError("output_ambiguous")
            digest, (data, mime) = next(iter(unique.items()))
            return data, mime, digest
    finally:
        generated_root = (_codex_home() / "generated_images").resolve()
        for path in outputs:
            try:
                if generated_root in path.resolve().parents:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        _RENDER_LOCK.release()


def _prune_async_jobs() -> None:
    cutoff = time.time() - ASYNC_JOB_TTL_SECONDS
    with _ASYNC_JOBS_LOCK:
        for job_id, job in list(_ASYNC_JOBS.items()):
            if float(job.get("created_at") or 0) < cutoff and job.get("status") in {"succeeded", "failed"}:
                _ASYNC_JOBS.pop(job_id, None)


def _run_async_job(job_id: str, request: RenderRequest) -> None:
    with _ASYNC_JOBS_LOCK:
        job = _ASYNC_JOBS.get(job_id)
        if not job:
            return
        job["status"] = "running"
    try:
        data, mime, digest = _render(request)
        with _ASYNC_JOBS_LOCK:
            if job_id in _ASYNC_JOBS:
                _ASYNC_JOBS[job_id].update({
                    "status": "succeeded",
                    "data": data,
                    "mime": mime,
                    "digest": digest,
                })
    except Exception as exc:
        with _ASYNC_JOBS_LOCK:
            if job_id in _ASYNC_JOBS:
                _ASYNC_JOBS[job_id].update({"status": "failed", "error": str(exc)[:200]})


def _async_job_response(job_id: str, job: dict):
    status = str(job.get("status") or "queued")
    if status in {"queued", "running"}:
        return JSONResponse({"job_id": job_id, "status": status}, status_code=202)
    if status == "failed":
        raise HTTPException(status_code=503, detail=str(job.get("error") or "codex_render_failed"))
    data = bytes(job.get("data") or b"")
    return Response(
        content=data,
        media_type=str(job.get("mime") or "image/png"),
        headers={
            "Cache-Control": "no-store",
            "X-Renderer": "codex-local",
            "X-Renderer-Version": APP_VERSION,
            "X-Render-Mode": str(job.get("mode") or ""),
            "X-Image-Sha256": str(job.get("digest") or ""),
        },
    )


@app.get("/health")
def health() -> dict:
    return readiness()


@app.post("/render")
async def render(
    request: RenderRequest,
    authorization: str | None = Header(default=None),
    x_renderer_token: str | None = Header(default=None),
) -> Response:
    _require_auth(authorization, x_renderer_token)
    try:
        data, mime, digest = await asyncio.to_thread(_render, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        code = str(exc)
        status = 429 if code == "renderer_busy" else 503
        raise HTTPException(status_code=status, detail=code) from exc
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Cache-Control": "no-store",
            "X-Renderer": "codex-local",
            "X-Renderer-Version": APP_VERSION,
            "X-Render-Mode": request.mode,
            "X-Image-Sha256": digest,
        },
    )


@app.post("/render-async", status_code=202)
def render_async(
    request: RenderRequest,
    authorization: str | None = Header(default=None),
    x_renderer_token: str | None = Header(default=None),
) -> JSONResponse:
    _require_auth(authorization, x_renderer_token)
    expected = EXPECTED_INPUTS[request.mode]
    if len(request.input_urls) != expected:
        raise HTTPException(status_code=400, detail="invalid_input_count")
    try:
        for url in request.input_urls:
            _validate_public_https_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not readiness()["ready"]:
        raise HTTPException(status_code=503, detail="renderer_not_ready")
    _prune_async_jobs()
    job_id = uuid.uuid4().hex
    with _ASYNC_JOBS_LOCK:
        _ASYNC_JOBS[job_id] = {
            "status": "queued",
            "created_at": time.time(),
            "mode": request.mode,
        }
    threading.Thread(target=_run_async_job, args=(job_id, request), daemon=True).start()
    return JSONResponse({"job_id": job_id, "status": "queued"}, status_code=202)


@app.get("/render-async/{job_id}")
def render_async_status(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_renderer_token: str | None = Header(default=None),
):
    _require_auth(authorization, x_renderer_token)
    _prune_async_jobs()
    with _ASYNC_JOBS_LOCK:
        job = dict(_ASYNC_JOBS.get(str(job_id)) or {})
    if not job:
        raise HTTPException(status_code=404, detail="render_job_not_found")
    return _async_job_response(str(job_id), job)

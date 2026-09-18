# 2026-09-18 — Codex quota policy and OpenAI fallback repair

- Repo: `lutzkind/etsy-image-renderer`
- Prior main: `2195062aa7a32ddfdb4b5aa7a349221e9681c769`
- Classification: `IMPLEMENTATION WITHIN APPROVED ARCHITECTURE` (explicit user
  instruction, 2026-09-18)
- Operations_MCP was not used.

## Root cause

The Responses API image fallback unconditionally added
`input_fidelity="high"` to reference-image (edit) requests. `gpt-image-2`
rejects that parameter, producing
`openai_image_fallback_http_400:invalid_input_fidelity_model`. Additionally,
the fallback was triggered automatically on confirmed Codex quota exhaustion,
which spent paid OpenAI API capacity without explicit operator authorization.

## Changes

1. Capability-aware request construction (`openai_fallback.py`):
   - per-model capability table (`gpt-image-2`, `gpt-image-1`);
   - only supported parameters are sent; `input_fidelity` is omitted for
     `gpt-image-2` and sent for reference edits where supported;
   - unknown image models fail closed
     (`openai_image_fallback_unsupported_image_model`) before any request;
   - generation and reference-edit operations both covered.
2. Explicit paid authorization (`openai_fallback.py`):
   - `ALLOW_PAID_OPENAI_IMAGE_FALLBACK` (default false) is the paid-spend
     boundary; `generate_image` refuses with
     `openai_image_fallback_not_authorized` when it is not set;
   - policy string `explicit_paid_authorization_only`.
3. Authoritative quota signal (`codex_quota.py`, `app.py`):
   - queries Codex app-server `account/rateLimits/read`;
   - classifies `available` / `exhausted` / `unknown` with bounded evidence;
   - exposed on `GET /quota`; `/health` is unchanged and does not spawn Codex.
4. Production spend gate (`app.py`):
   - Codex quota exhaustion returns the typed `codex_quota_unavailable`
     unless paid fallback is explicitly authorized;
   - non-quota Codex failures (timeout, 429, 5xx, auth, transport) never use
     the paid fallback.
5. Deployment: `Dockerfile` copies `codex_quota.py`; `docker-compose.yaml`
   carries `ALLOW_PAID_OPENAI_IMAGE_FALLBACK=${...:-false}`.

## Verification

- `pytest -q` -> 121 passed; `ruff check .` clean.
- Zero-cost fallback certification harness
  (`tests/test_fallback_certification.py`) exercises production request
  construction and response handling with a mocked provider:
  - explicit authorization + Codex unavailable -> well-formed `gpt-image-2`
    request without `input_fidelity`, mocked valid raster accepted;
  - production default + Codex unavailable -> paid fallback never invoked.
- Fault coverage: HTTP 400/401/403/429/5xx, transport timeout, malformed JSON,
  missing/ambiguous/malformed/oversized output, retry after transport failure,
  unsupported model parameter prevention, authorization truthy/falsy values.
- `real paid provider invocation not performed by policy` ($0 OpenAI API
  image-generation spend).

# Rolling current GPT Image model policy — 2026-10-05

Status: implemented (renderer repository).  Canonical approval and the project
decision record live in `lutzkind/etsy-automation` (D-081); this file records
the renderer-side design and evidence.

## Context

The renderer previously hard-coded `gpt-image-2` into primary-path provenance
and into the paid API fallback default, and the paid fallback used a static
exact-name capability allowlist.  That made every OpenAI image-model release
require a renderer source edit and deployment, and made
`X-Image-Model: gpt-image-2` non-authoritative evidence.

The approved end state is a rolling policy: the normal path uses Codex's
built-in image-generation capability with Codex's current supported/default
GPT Image model, provenance is truthful, and the explicitly authorized paid
fallback automatically resolves the newest stable full-capability GPT Image
model without a per-release code change.

## Primary Codex path

- Invocation remains `codex app-server --enable image_generation --listen
  stdio://` with no numbered image model pin.
- The Codex app-server protocol exposes no image model on the
  `imageGeneration` thread item (verified by generating the protocol JSON
  schemas for the deployed 0.145.0 and the newer 0.160.0 releases: the item has
  `id`, `result`, `revisedPrompt`, `savedPath`, `status`, `type`, and in newer
  versions `failure`/`transparentBackground` — no model field).  Therefore the
  renderer records the actual model only if a future event exposes one, and
  otherwise reports provider-managed selection truthfully.
- There is no supported official "latest" image-model selector to use instead;
  leaving the model unspecified is the correct unversioned behavior.

Provenance contract for the primary path:

```json
{
  "provider": "codex-image",
  "fallback_used": false,
  "fallback_reason": "",
  "model": "provider-selected" | "<actual model when exposed>",
  "model_observed": false,
  "model_selection_policy": "codex-provider-default",
  "model_configured_override": "",
  "model_source": "codex-provider-managed" | "codex-app-server-event"
}
```

The same metadata is served on the synchronous `X-Image-Model*` headers, async
job metadata/status, async result headers, `/health`, and the fresh-render
proof.  Legacy persisted async jobs carrying the old fabricated `gpt-image-2`
label are normalized to `provider-selected` on restore.

## Paid OpenAI API fallback

Disabled by default (`ALLOW_PAID_OPENAI_IMAGE_FALLBACK` absent/false); Codex
quota exhaustion never authorizes spend.  When explicitly authorized:

- `OPENAI_IMAGE_FALLBACK_IMAGE_MODEL` unset/empty/`auto` = automatic
  resolution; an exact value is an operator pin for emergency rollback/testing.
- Automatic resolution uses the account's authoritative `GET /v1/models`
  catalog (zero-cost) with a bounded in-process cache
  (`OPENAI_IMAGE_MODEL_CATALOG_TTL_SECONDS`, default 3600, clamp 60–86400) and
  a bounded request timeout (`OPENAI_IMAGE_MODEL_CATALOG_TIMEOUT_SECONDS`,
  default 10, clamp 2–60).  Resolution happens only at the spend boundary
  (`generate_image`), never in `/health`.
- Selection: parse `gpt-image-<numeric version>[-<variant>][-<date>]`;
  exclude non-GPT-Image/deprecated aliases (`chatgpt-image-latest`), and
  `mini`/reduced-capability/preview/experimental identifiers; skip dated
  snapshots when the undated stable alias exists; rank by numeric version
  (`2.10 > 2.9`), then documented variant capability
  (`sunburst` > undecorated/unknown > `flare`), then `created` as a tiebreak,
  then id.  The current live account resolves to `gpt-image-2.5-sunburst`.
- Failure behavior: catalog outage -> last successfully resolved model ->
  documented baseline `gpt-image-2.5-sunburst`; the resolution status exposes
  `resolution_source` (`catalog`/`cache`/`last_known`/`baseline`/`explicit`)
  and a bounded `catalog_error`.  It never regresses to an obsolete Image 1
  family model and never converts catalog uncertainty into spend approval.
- Request construction: capabilities are derived from the parsed release
  version.  `input_fidelity` is sent only for GPT Image 1.x and earlier; GPT
  Image 2 and later (including unknown future releases) omit it.  A new normal
  `gpt-image-*` release no longer fails because its exact name is absent from a
  static dictionary; a non-GPT-Image model still fails closed before any
  request.

## Observability (non-secret)

`GET /health` adds: `primary_image_provider`, `primary_image_model`
(`provider-selected`), `primary_image_model_selection_policy`
(`codex-provider-default`), `primary_image_model_pinned` (false),
`api_fallback_image_model_policy` (`auto`/`explicit`),
`api_fallback_image_model_source`, `api_fallback_image_model_override`,
`api_fallback_image_model_baseline`, `api_fallback_image_model_catalog_error`,
and per-mode `fresh_proof_provenance`.

## Caller compatibility

The Etsy caller persists and validates its own gallery provenance through
non-repairable runtime Windmill scripts (`f/etsy/gallery-builder`,
`f/etsy/gallery-initial-renderer`).  It consumes the unchanged async response
shape (`provider`, `fallback_used`, `fallback_reason`, `model`,
`output_sha256`, `result_url`) and does not require the renderer's model label
to be a numbered version; the tracked caller validation still uses its own
persisted `gpt-image-2` contract.  No Etsy contract, quota policy, idempotency,
or QA boundary changes.

## Simplicity boundary

No scheduler, cron, registry service, proxy, database, watchdog, or automated
commit was added.  Resolution is a small bounded in-process function inside the
existing fallback module.

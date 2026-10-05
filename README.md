# etsy-codex-renderer

Private image renderer for the Windmill Etsy automation pipeline. Codex's built-in image-generation capability — using its current supported/default GPT Image model — is the only customer-facing raster renderer. The renderer does not pin a numbered GPT Image generation model for its normal Codex path. Windmill owns state, provenance, independent Luna QA, approval, and final listing decisions; this service never draws or assembles customer-facing pixels.

The renderer uses the same server Codex session as the Codex proxy. The host
Codex home is mounted as a directory and the renderer reads
`/run/secrets/codex-session/auth.json`; it is deliberately not mounted as a
single file. This preserves visibility of an atomic refresh-token replacement
across restarts and prevents a missing source file from becoming a Docker
directory mount.

`POST /render` and `POST /render-async` accept the versioned `luxlm-render-contract-v5-codex-final-raster-personalization-contract`.

The `personalization_examples` designed-card module accepts the frozen Etsy
Make It Yours State A/B/C contract. Blank selectors preserve the purchased
listing style; the renderer rejects artist-discretion/default-picker fields,
and the obsolete single-dimension selector modules are disabled. The combined
State A/B/C card is the only current personalization-card path.
background-only contracts, and option definitions without their frozen visual
references.

## Modes

- `minimal_frame`: Codex-generated complete minimal frame hero.
- `lifestyle`: Codex-generated complete lifestyle/editorial raster from the scene reference and listing artwork.
- `orientation`: Codex-generated complete artwork presentation.
- `decorative_asset`: existing decorative generation mode. It remains strictly no-text: no letters, numbers, signatures, logos, watermarks, captions, labels, or empty text-bearing panels. New requests must provide `asset_roles`, an exact `expected_input_count`, module/template identity, and explicit prohibitions. Use `exact_pixel_preservation=true` when the supplied artwork raster must remain exact.
- `designed_card`: generates a complete premium card. All visible copy must be taken verbatim from the approved `card_brief`; the renderer must not invent, rewrite, translate, or add text. `template_reference_url` is inspiration-only, is passed separately from the `card_brief`, and is not an authority for copy.

Example decorative request:

```json
{
  "mode": "decorative_asset",
  "module": "photo_guide",
  "template_family": "correct_wrong_photo_guide_v1",
  "expected_input_count": 2,
  "asset_roles": [
    {"role": "source_photo", "url": "https://...", "preservation": "subject_identity"},
    {"role": "style_anchor", "url": "https://...", "preservation": "style_only"}
  ],
  "generation_instructions": {"decorative_density": "low"},
  "prohibited_elements": ["text", "letters", "numbers", "signature", "logo", "watermark"],
  "prompt_version": "luxlm-decorative-asset-v1"
}
```

## Async behavior

`POST /render-async` returns a job identifier. Duplicate requests with the same normalized request return the same job rather than creating another render.

- Status JSON: `GET /render-async/{job_id}`
- Result binary: `GET /render-async/{job_id}/result`

Successful render responses include the renderer version, contract version, request hash, and output SHA-256.

## Image provider policy

The **primary path** uses Codex's built-in `image_generation` capability (via
the shared Codex session) with no numbered GPT Image model pinned. Codex's
supported service selects its current/default GPT Image model, so a newer
Images release does not require a renderer source change or deployment merely
to be usable. There is **no automatic paid fallback**:

- Provenance is truthful. The renderer records the actual image model when the
  Codex app-server event exposes one; otherwise it reports
  `provider: codex-image`, `model: provider-selected`, and
  `model_selection_policy: codex-provider-default` — it never fabricates a
  numbered model. The same metadata is returned on `X-Image-Model*` headers,
  async job status, async result headers, `/health`, and the fresh-render
  proof.
- Included Codex image capacity is required. When the authoritative
  `account/rateLimits/read` signal reports `exhausted`, `/render-async` fails
  with the typed error `codex_quota_unavailable` and the production daily path
  returns a healthy zero-cost skip. The renderer never silently spends.
- `GET /quota` returns the structured quota state
  (`available` / `exhausted` / `unknown`), the resolved plan, window usage,
  credit status, and whether paid fallback is authorized. It is separate from
  `/health` so the container healthcheck stays fast.
- The OpenAI Images API fallback remains implemented and functional, but runs
  only when `ALLOW_PAID_OPENAI_IMAGE_FALLBACK=true` is explicitly set
  (default `false`). Codex quota exhaustion alone never authorizes it.
- Fallback model selection is automatic by default. With
  `OPENAI_IMAGE_FALLBACK_IMAGE_MODEL` unset (or `auto`), the renderer resolves
  the newest stable full-capability GPT Image model from the account's
  authoritative `GET /v1/models` catalog using a bounded in-process cache.
  Dated snapshots, deprecated aliases, `mini`/reduced-capability models, and
  preview/experimental models are never chosen. Version ordering is numeric
  (`2.10 > 2.9`), so a future normal `gpt-image-3` supersedes 2.5 without a
  source change. Set an exact model (for example `gpt-image-2.5-sunburst`) to
  pin for emergency rollback/testing. A catalog outage degrades safely to the
  last successful resolution, then to the documented baseline
  `gpt-image-2.5-sunburst`; it never regresses to an obsolete Image 1 model
  and never turns catalog uncertainty into spend authorization.
- Fallback request construction is capability-aware from the parsed release
  version: only parameters supported by the selected model and operation are
  sent (`input_fidelity` is omitted for GPT Image 2 and later; earlier GPT
  Image models keep it). A non-GPT-Image model fails closed before any request
  is made, while a new normal `gpt-image-*` release is accepted without a
  static exact-name allowlist entry.

Certification of the fallback is deterministic and zero-cost: unit and
integration tests mock only the provider boundary and never make a billable
image-generation request.

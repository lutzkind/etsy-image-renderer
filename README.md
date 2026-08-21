# etsy-codex-renderer

Private image renderer for the Windmill Etsy automation pipeline. Codex Image 2 is the only customer-facing raster renderer. Windmill owns state, provenance, independent Luna QA, approval, and final listing decisions; this service never draws or assembles customer-facing pixels.

The renderer uses the same server Codex session as the Codex proxy. The host
Codex home is mounted as a directory and the renderer reads
`/run/secrets/codex-session/auth.json`; it is deliberately not mounted as a
single file. This preserves visibility of an atomic refresh-token replacement
across restarts and prevents a missing source file from becoming a Docker
directory mount.

`POST /render` and `POST /render-async` accept the versioned `luxlm-render-contract-v4-codex-only-final-raster`.

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

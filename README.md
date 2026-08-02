# etsy-codex-renderer

Dedicated private local-Codex image renderer for the Windmill Etsy automation
pipeline. The service is an isolated visual-asset boundary: it does not own
listing state or final customer-facing typography.

`POST /render` and `/render-async` accept the versioned
`luxlm-render-contract-v2`. Legacy modes remain available with their exact
input counts. New decorative requests must provide `asset_roles`, an exact
`expected_input_count`, a module/template identity, and strict no-text
prohibitions. Exact artwork rasters can be marked
`exact_pixel_preservation=true`; deterministic Windmill compositors remain
responsible for final placement and all exact wording.

Example request shape:

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
  "prohibited_elements": [
    "text", "letters", "numbers", "signature", "logo", "watermark",
    "blank_caption_sheet", "paper_mat", "marketing_panel", "empty_label_region"
  ],
  "prompt_version": "luxlm-decorative-asset-v1"
}
```

Every successful response includes the renderer version, contract version,
request hash, and output SHA-256. Identical requests are rejected for the
bounded cache window so a retry cannot silently pay for an identical render.

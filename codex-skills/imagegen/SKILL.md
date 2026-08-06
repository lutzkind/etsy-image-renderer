---
name: "imagegen"
description: "Use the built-in image generation tool to produce the requested raster asset."
---

# Renderer image generation contract

For this renderer, the caller supplies the complete listing-specific prompt and
reference images. Use the built-in `image_gen` / `image_generation` tool exactly
once, wait for its raster result, and copy the exact generated raster to
`./rendered-output.png`.

Do not use an external image API, an API key, a fallback script, SVG, HTML, CSS,
or programmatic drawing. Do not return a prose-only success. If the built-in
image-generation tool is unavailable, stop with a nonzero error.

Preserve the roles and pixel-preservation instructions in the caller prompt.
For text-bearing cards, use only the exact caller-supplied copy; never invent,
paraphrase, or add visible text.

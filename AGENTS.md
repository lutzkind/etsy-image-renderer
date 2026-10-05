# AGENTS.md — etsy-image-renderer

Purpose: Codex final-raster renderer for the Etsy automation pipeline; the
current supported/default GPT Image model is selected by Codex's built-in
image-generation capability and is not pinned per release.

GitHub: `lutzkind/etsy-image-renderer` · Canonical checkout: `/root/etsy-image-renderer`
· Default branch: `main`.

## Start here

- Docs: `README.md` (contract, modes, image provider policy) and `docs/`
  (dated repair/design records, including the rolling current-GPT-Image model
  policy).
- This repository is renderer implementation only. Etsy governance,
  architecture, orchestrator, and Windmill sources live in
  `lutzkind/etsy-automation` (`/root/etsy-automation`).
- Host map: `/root/REPO_MAP.md` (canonical checkouts, duplicates, production).
- `/root/mcp-shared/chatgpt/**` is continuity/history evidence, not the source
  of truth. `/root/agent-tmp/**` is disposable scratch; `/root/backups/**` is
  retired copies. Do not treat them as canonical.

## Branch / state rule

- Local checkout is not proof of `origin/main`; `origin/main` is not proof of
  production. Check `git status -sb`, compare with `git rev-parse origin/main`,
  and inspect live production read-only when the task depends on deployed
  state.
- Do not switch, reset, pull, merge, or clean without task authorization.
- CI (`.github/workflows/ci.yml`) runs on pull requests: the renderer-only
  boundary check, the full pytest suite, and a Docker build.

## Commands

| Purpose | Command |
|---|---|
| Install | `pip install -r requirements-dev.txt` |
| Targeted test | `python -m pytest tests/test_model_selection.py` |
| Full suite | `python -m pytest -q` |
| Build | `docker build --build-arg CODEX_VERSION=0.145.0 -t etsy-codex-renderer:test .` |

## Production

- Coolify app `etsy-codex-renderer` (`fwxnnc9hd9288dt66wqte5x2`), repository
  `lutzkind/etsy-image-renderer`, branch `main`. Git push to `main` is the
  deployment source; verify the deployed image tag/commit after any deploy.
- The container is isolated by its read-only, capability-dropped Docker
  boundary; Codex's nested sandbox is deliberately full-access inside it
  because the built-in image tool must read local reference files.
- Environment policy: `ALLOW_PAID_OPENAI_IMAGE_FALLBACK` stays unset/false in
  production; `OPENAI_IMAGE_FALLBACK_IMAGE_MODEL` should be unset or `auto`
  (an exact value is emergency rollback/testing only). `/quota` is the
  authoritative Codex quota surface; `/health` deliberately does not spawn a
  Codex process.
- Production verification: `GET /health` (selection/provenance policy),
  `GET /quota`, and a real authorized render's returned provenance. Never
  infer production from Git state alone.

## Traps

- Do not pin a numbered GPT Image model in the primary Codex path or fabricate
  one in provenance. The primary path reports `provider-selected` with policy
  `codex-provider-default` unless the Codex event exposes a real model.
- Do not make `GET /health` probe Codex or the OpenAI model catalog; both must
  stay fast and side-effect-free.
- Paid OpenAI image generation is explicit-authorization-only. Codex quota
  exhaustion never authorizes spend.
- `input_fidelity` must not be sent for GPT Image 2 and later; capability is
  derived from the parsed release version, not a static exact-name allowlist.
- `RENDER_DATA_DIR` holds the fresh-render proof and async jobs; it is a
  Docker volume, so proofs survive restarts. A pipeline-version bump
  invalidates the proof and requires fresh renders.

## Do not read/search by default

- `/root/agent-tmp/**` (disposable scratch), `/root/mcp-shared/chatgpt/**`
  (history, not source of truth), `/root/backups/**` (retired copies).

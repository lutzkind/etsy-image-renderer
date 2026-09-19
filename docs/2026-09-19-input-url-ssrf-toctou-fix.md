# 2026-09-19 — PA-83 input URL download SSRF TOCTOU fix

- Repo: `lutzkind/etsy-image-renderer`
- Prior main: `fc24f6a88343f268affd559758a78d2e9e990a22`
- Classification: `IMPLEMENTATION WITHIN APPROVED ARCHITECTURE`
- Finding: portfolio audit PA-83 (severity Medium -> Low; requires a valid
  renderer token and attacker-controlled DNS)

## Revalidated at fc24f6a

- `app.py:344-356` (`_public_addresses`) resolved the hostname and rejected
  private/loopback/link-local/multicast/reserved/unspecified addresses.
- `app.py:359-368` (`_validate_public_https_url`) returned the
  hostname-bearing URL without pinning the validated address.
- `app.py:381-400` (`_download_image`) validated and then called
  `httpx.Client(...).get(current)`, which re-resolved DNS. A rebinding name
  could resolve public during validation and private at fetch. Redirects were
  re-validated per hop (`:386-391`, max 4) but kept the same window.
- Auth prerequisite: `/render` and `/render-async` call `_require_auth`; no
  unauthenticated path exists.

## Fix

1. `_validated_public_url` now returns the validated URL plus its resolved
   public address list; `_validate_public_https_url` remains the URL-only
   wrapper used by the route preflight, so route behavior is unchanged.
2. `_PinnedAddressHTTPSConnection` subclasses `http.client.HTTPSConnection`
   and dials only those pre-validated addresses. SNI and certificate
   verification still use the original hostname (`server_hostname`), so TLS
   validation is unchanged and DNS is never consulted at fetch time. Each
   redirect hop is validated and pinned independently; at most four fetches.
3. `_pinned_https_get` reads at most `MAX_INPUT_BYTES + 1` bytes, so the
   input-size cap now also bounds memory instead of only the written artifact.
4. Rasters are written to a same-directory `mkstemp` `.part` file, flushed,
   `fsync`-ed, and committed with `os.replace`. Failed, oversize, or
   non-raster downloads leave no partial artifact in the render workspace.
5. Upstream HTTP error statuses raise `_InputHttpError` (an unmapped 500),
   preserving the previous non-ValidationError endpoint behavior.

## Residual

- Exploitation of the original TOCTOU required a valid renderer token plus
  attacker-controlled DNS; there is no unauthenticated path, and the rebinding
  window is now closed.
- An authenticated caller can still ask the renderer to fetch arbitrary
  *public* HTTPS URLs. That is the intended input feature, not private-range
  SSRF; private, loopback, link-local, multicast, reserved, and unspecified
  targets remain rejected.
- External exposure of the private renderer service remains an infrastructure
  question and is unchanged by this fix.
- All tests use deterministic fakes and contact no network.

## Tests

`tests/test_input_url_security.py` (28 tests): URL-shape and local-host
rejection, private/loopback/link-local/unspecified/IPv4-mapped-IPv6/mixed
resolution rejection, single-resolution pinning, pinned dial with hostname
SNI, address fallback, DNS-rebinding regression, atomic replace, oversize and
unsupported-raster cleanup, typed upstream HTTP errors, and redirect
revalidation / private-target / missing-location / redirect-loop failure.

`python -m pytest -q`: 151 passed (123 pre-existing + 28 new).

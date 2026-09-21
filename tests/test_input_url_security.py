"""PA-83 — pinned-address input downloads close the DNS-rebinding TOCTOU.

The URL validation boundary resolves the hostname and rejects any non-public
address. Before this repair the download then re-resolved the hostname through
httpx, so a rebinding name could validate as public and connect to a private
address. These tests prove that the download path now connects only to the
exact addresses accepted during validation (with the original hostname kept for
SNI/certificate verification) and that rasters are committed atomically via a
temp file plus ``os.replace``.

All tests are offline; DNS and sockets are replaced by deterministic fakes.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as renderer


PNG = b"\x89PNG\r\n\x1a\npinned-download"


def _addr(*addresses: str, port: int = 443):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in addresses]


def _fake_context(events: list):
    class FakeContext:
        def wrap_socket(self, raw_socket, server_hostname=None):
            events.append(("wrap", server_hostname))
            return "tls-socket"

    return FakeContext()


# -- URL validation boundary -------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/art.png",
        "ftp://example.com/art.png",
        "https://user:secret@example.com/art.png",
        "https://example.com/art.png#fragment",
        "https:///art.png",
    ],
)
def test_non_https_or_disallowed_url_shape_is_rejected(url):
    with pytest.raises(ValueError, match="invalid_input_url"):
        renderer._validate_public_https_url(url)


@pytest.mark.parametrize("host", ["localhost", "api.localhost", "printer.local"])
def test_local_hosts_are_rejected_before_any_dns_lookup(host, monkeypatch):
    def fail_dns(*args, **kwargs):
        raise AssertionError("local hosts must not be resolved")

    monkeypatch.setattr(renderer.socket, "getaddrinfo", fail_dns)
    with pytest.raises(ValueError, match="input_url_not_public"):
        renderer._validate_public_https_url(f"https://{host}/art.png")


@pytest.mark.parametrize(
    "address",
    ["10.0.0.8", "127.0.0.1", "169.254.10.10", "0.0.0.0", "::1", "::ffff:127.0.0.1", "fd00::1"],
)
def test_non_public_resolved_addresses_are_rejected(address, monkeypatch):
    monkeypatch.setattr(renderer.socket, "getaddrinfo", lambda *args, **kwargs: _addr(address))
    with pytest.raises(ValueError, match="input_url_not_public"):
        renderer._public_addresses("public.example")


def test_any_private_address_in_a_mixed_resolution_rejects_the_host(monkeypatch):
    monkeypatch.setattr(
        renderer.socket, "getaddrinfo", lambda *args, **kwargs: _addr("93.184.216.34", "10.0.0.9")
    )
    with pytest.raises(ValueError, match="input_url_not_public"):
        renderer._public_addresses("mixed.example")


def test_unresolvable_host_is_rejected(monkeypatch):
    def fail_dns(*args, **kwargs):
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(renderer.socket, "getaddrinfo", fail_dns)
    with pytest.raises(ValueError, match="input_host_unresolvable"):
        renderer._public_addresses("missing.example")


def test_validated_url_returns_the_pinned_addresses_with_one_resolution(monkeypatch):
    resolutions = []

    def fake_getaddrinfo(host, port, type=0):
        resolutions.append((host, port))
        return _addr("93.184.216.34")

    monkeypatch.setattr(renderer.socket, "getaddrinfo", fake_getaddrinfo)
    url, addresses = renderer._validated_public_url("https://cdn.example/art.png")
    assert url == "https://cdn.example/art.png"
    assert addresses == ["93.184.216.34"]
    assert resolutions == [("cdn.example", 443)]


# -- pinned connect ----------------------------------------------------------

def test_pinned_connection_dials_validated_address_and_keeps_hostname_sni(monkeypatch):
    events = []

    class FakeSocket:
        def close(self):
            events.append(("close",))

    monkeypatch.setattr(
        renderer.socket,
        "create_connection",
        lambda address, timeout=None: (events.append(("connect", address, timeout)), FakeSocket())[1],
    )
    monkeypatch.setattr(
        renderer.socket,
        "getaddrinfo",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no re-resolution")),
    )
    connection = renderer._PinnedAddressHTTPSConnection(
        "rebind.example", 443, ["93.184.216.34", "93.184.216.35"], timeout=5, context=_fake_context(events)
    )
    connection.connect()
    assert events == [("connect", ("93.184.216.34", 443), 5), ("wrap", "rebind.example")]
    assert connection.sock == "tls-socket"


def test_pinned_connection_falls_back_across_validated_addresses(monkeypatch):
    events = []
    dialed = []

    class FakeSocket:
        def close(self):
            events.append(("close",))

    def create_connection(address, timeout=None):
        dialed.append(address)
        if address[0] == "93.184.216.34":
            raise OSError("connection refused")
        return FakeSocket()

    monkeypatch.setattr(renderer.socket, "create_connection", create_connection)
    connection = renderer._PinnedAddressHTTPSConnection(
        "rebind.example", 443, ["93.184.216.34", "2001:db8::10"], timeout=2, context=_fake_context(events)
    )
    connection.connect()
    assert dialed == [("93.184.216.34", 443), ("2001:db8::10", 443)]
    assert connection.sock == "tls-socket"


def test_rebinding_name_cannot_switch_to_a_private_ip_after_validation(monkeypatch):
    responses = [_addr("93.184.216.34"), _addr("127.0.0.1")]
    resolutions = []

    def fake_getaddrinfo(host, port, type=0):
        resolutions.append(host)
        return responses[min(len(resolutions) - 1, 1)]

    monkeypatch.setattr(renderer.socket, "getaddrinfo", fake_getaddrinfo)
    _url, addresses = renderer._validated_public_url("https://rebind.example/art.png")
    assert addresses == ["93.184.216.34"]

    dialed = []

    def create_connection(address, timeout=None):
        dialed.append(address)
        raise OSError("connection refused")

    monkeypatch.setattr(renderer.socket, "create_connection", create_connection)
    connection = renderer._PinnedAddressHTTPSConnection(
        "rebind.example", 443, addresses, timeout=2, context=_fake_context([])
    )
    with pytest.raises(OSError):
        connection.connect()
    assert dialed == [("93.184.216.34", 443)]
    assert resolutions == ["rebind.example"], "the fetch must not re-resolve the hostname"


# -- atomic download ---------------------------------------------------------

def test_download_uses_pinned_addresses_and_atomically_replaces(monkeypatch, tmp_path):
    fetches = []
    replaced = []
    monkeypatch.setattr(renderer, "_validated_public_url", lambda value: (value, ["93.184.216.34"]))
    monkeypatch.setattr(
        renderer,
        "_pinned_https_get",
        lambda url, addresses: fetches.append((url, tuple(addresses)))
        or (200, {"content-type": "image/png"}, PNG),
    )
    real_replace = renderer.os.replace
    monkeypatch.setattr(
        renderer.os,
        "replace",
        lambda src, dst: (replaced.append((Path(src), Path(dst))), real_replace(src, dst))[1],
    )
    result = renderer._download_image("https://cdn.example/art.png", tmp_path / "input-1")
    assert result == tmp_path / "input-1.png"
    assert result.read_bytes() == PNG
    assert fetches == [("https://cdn.example/art.png", ("93.184.216.34",))]
    assert len(replaced) == 1
    temp_path, final_path = replaced[0]
    assert temp_path.suffix == ".part"
    assert final_path == result
    assert list(tmp_path.glob("*.part")) == []


@pytest.mark.parametrize(
    "body, error",
    [
        (b"\x89PNG\r\n\x1a\n" + b"x" * (renderer.MAX_INPUT_BYTES + 1), "input_image_too_large"),
        (b"not-a-raster", "unsupported_image"),
    ],
)
def test_rejected_download_leaves_no_artifact(monkeypatch, tmp_path, body, error):
    monkeypatch.setattr(renderer, "_validated_public_url", lambda value: (value, ["93.184.216.34"]))
    monkeypatch.setattr(renderer, "_pinned_https_get", lambda url, addresses: (200, {}, body))
    with pytest.raises(ValueError, match=error):
        renderer._download_image("https://cdn.example/art.png", tmp_path / "input-1")
    assert list(tmp_path.iterdir()) == []


def test_upstream_http_error_is_typed_and_not_a_url_validation_error(monkeypatch, tmp_path):
    monkeypatch.setattr(renderer, "_validated_public_url", lambda value: (value, ["93.184.216.34"]))
    monkeypatch.setattr(renderer, "_pinned_https_get", lambda url, addresses: (503, {}, b""))
    with pytest.raises(renderer._InputHttpError, match="input_http_503"):
        renderer._download_image("https://cdn.example/art.png", tmp_path / "input-1")
    assert list(tmp_path.iterdir()) == []


def test_redirect_hops_are_revalidated_and_pinned_per_hop(monkeypatch, tmp_path):
    validated = []

    def fake_validated(value):
        validated.append(value)
        return value, [f"93.184.216.{len(validated)}"]

    def fake_get(url, addresses):
        if url == "https://cdn.example/redirect.png":
            return 302, {"location": "https://cdn2.example/final.png"}, b""
        return 200, {}, PNG

    monkeypatch.setattr(renderer, "_validated_public_url", fake_validated)
    monkeypatch.setattr(renderer, "_pinned_https_get", fake_get)
    result = renderer._download_image("https://cdn.example/redirect.png", tmp_path / "input")
    assert validated == ["https://cdn.example/redirect.png", "https://cdn2.example/final.png"]
    assert result.read_bytes() == PNG


def test_redirect_to_a_private_target_is_rejected(monkeypatch, tmp_path):
    def fake_validated(value):
        if "private" in value:
            raise ValueError("input_url_not_public")
        return value, ["93.184.216.34"]

    monkeypatch.setattr(renderer, "_validated_public_url", fake_validated)
    monkeypatch.setattr(
        renderer, "_pinned_https_get", lambda url, addresses: (302, {"location": "https://private.local/x.png"}, b"")
    )
    with pytest.raises(ValueError, match="input_url_not_public"):
        renderer._download_image("https://cdn.example/art.png", tmp_path / "input")


def test_redirect_without_location_and_excess_redirects_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(renderer, "_validated_public_url", lambda value: (value, ["93.184.216.34"]))
    monkeypatch.setattr(renderer, "_pinned_https_get", lambda url, addresses: (302, {}, b""))
    with pytest.raises(ValueError, match="invalid_input_redirect"):
        renderer._download_image("https://cdn.example/art.png", tmp_path / "no-location")

    monkeypatch.setattr(
        renderer, "_pinned_https_get", lambda url, addresses: (307, {"location": "/next"}, b"")
    )
    with pytest.raises(ValueError, match="too_many_input_redirects"):
        renderer._download_image("https://cdn.example/art.png", tmp_path / "loop")
    assert list(tmp_path.iterdir()) == []

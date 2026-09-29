"""Fuzz the installer's HTTP/1.1 response reader -- status line, headers, and the
chunked / Content-Length / close-delimited body framing -- over a scripted socket.

These bytes come from whatever answers the TLS connection: the update server, a CDN the
manifest points at, or a captive portal. The contract: the reader returns the body or raises
``ValueError``/``OSError`` (the installer's documented pre-erase failures), and never lets the
wire decide how much it allocates."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from tests.fuzz import fuzzlib as F

INST = F.load_installer()
_LIMIT = 8192


def _recv_from(data: bytes, cuts):
    """A ``recv(n)`` over ``data`` that hands out uneven pieces (``cuts`` are piece sizes), then
    ``b''`` (EOF) -- TLS records arrive in whatever sizes the link produces."""
    state = {"off": 0, "i": 0}

    def recv(n):
        off = state["off"]
        if off >= len(data):
            return b""
        k = cuts[state["i"] % len(cuts)] if cuts else n
        state["i"] += 1
        k = max(1, min(k, n))
        state["off"] = off + k
        return data[off:off + k]
    return recv


def fetch(raw: bytes, cuts=()):
    """``_read_response`` + ``_make_body`` + ``_read_all``: what ``_fetch_manifest`` does to a
    response. Returns ``(code, body)``."""
    reader = INST._Reader(_recv_from(raw, list(cuts)))
    code, headers = INST._read_response(reader)
    return code, INST._read_all(INST._make_body(reader, headers), _LIMIT)


def _chunked(body: bytes, sizes) -> bytes:
    out, i = bytearray(), 0
    for s in sizes:
        if i >= len(body):
            break
        piece = body[i:i + s]
        out += b"%x\r\n" % len(piece) + piece + b"\r\n"
        i += len(piece)
    if i < len(body):
        out += b"%x\r\n" % (len(body) - i) + body[i:] + b"\r\n"
    return bytes(out) + b"0\r\n\r\n"


_cuts = st.lists(st.integers(1, 64), max_size=6)


@given(st.binary(max_size=3000), st.lists(st.integers(1, 700), min_size=1, max_size=8), _cuts)
def test_chunked_bodies_round_trip(body, sizes, cuts):
    raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + _chunked(body, sizes)
    assert fetch(raw, cuts) == (200, body)


@given(st.binary(max_size=3000), _cuts)
def test_content_length_bodies_round_trip(body, cuts):
    raw = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body + b"extra"
    assert fetch(raw, cuts) == (200, body)


@given(st.binary(max_size=3000), _cuts)
def test_close_delimited_bodies_round_trip(body, cuts):
    raw = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + body
    assert fetch(raw, cuts) == (200, body)

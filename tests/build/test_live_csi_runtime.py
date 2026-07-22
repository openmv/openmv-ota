"""Host tests for the device-side ``openmv_cloud.csi`` runtime module (the async
camera with OpenMV Live built in, scaffolded into ``app/lib/openmv_cloud/``).

Pure logic only: the WebSocket codec (pinned against RFC 6455 by construction
and against the relay's expectations), URL parsing, the throttle, the
control-message session, the stream registry, grant plumbing, and the
disposal-time frame tee (driven with a fake builtin camera + asyncio on the
host). The network entry points wire MicroPython-only I/O and are covered on
hardware, not here.
"""

from __future__ import annotations

import asyncio
import json
import struct

import pytest

from openmv_ota.build.device.openmv_cloud import csi as rt


@pytest.fixture(autouse=True)
def _fresh_module_state():
    rt._streams.clear()
    rt.set_grant(None)
    yield
    rt._streams.clear()
    rt.set_grant(None)


# --- URL parsing -----------------------------------------------------------------

@pytest.mark.parametrize(("url", "want"), [
    ("wss://live.cloud.openmv.io/camera/d1/0?token=t",
     (True, "live.cloud.openmv.io", 443, "/camera/d1/0?token=t")),
    ("https://live.cloud.openmv.io/poll/d1/tele?token=t",
     (True, "live.cloud.openmv.io", 443, "/poll/d1/tele?token=t")),
    ("ws://localhost:8787/camera/d1/0?token=t",
     (False, "localhost", 8787, "/camera/d1/0?token=t")),
    ("http://relay:8080/poll/d1/0", (False, "relay", 8080, "/poll/d1/0")),
])
def test_split_url(url, want):
    assert rt._split_url(url) == want


@pytest.mark.parametrize("bad", ["ftp://x/y", "no-scheme", "https://"])
def test_split_url_rejects(bad):
    with pytest.raises(ValueError):
        rt._split_url(bad)


# --- WebSocket codec ---------------------------------------------------------------

def _unmask(frame, header_len):
    key = frame[header_len:header_len + 4]
    body = bytearray(frame[header_len + 4:])
    for i in range(len(body)):
        body[i] ^= key[i & 3]
    return bytes(body)


def test_encode_frame_small_masked_round_trip():
    frame = rt._encode_frame(rt._OP_BINARY, b"\xff\xd8jpeg", b"\x01\x02\x03\x04")
    fin, opcode, masked, len7 = rt._decode_header(frame[0], frame[1])
    assert (fin, opcode, masked, len7) == (True, rt._OP_BINARY, True, 6)
    assert _unmask(frame, 2) == b"\xff\xd8jpeg"


def test_encode_frame_medium_uses_16bit_length():
    payload = bytes(300)
    frame = rt._encode_frame(rt._OP_BINARY, payload, b"\x00\x00\x00\x00")
    _fin, _op, _m, len7 = rt._decode_header(frame[0], frame[1])
    assert len7 == 126
    assert struct.unpack("!H", frame[2:4])[0] == 300
    assert _unmask(frame, 4) == payload   # zero mask key = identity


def test_encode_frame_large_uses_64bit_length():
    payload = bytes(70000)
    frame = rt._encode_frame(rt._OP_BINARY, payload, b"\x00\x00\x00\x00")
    _fin, _op, _m, len7 = rt._decode_header(frame[0], frame[1])
    assert len7 == 127
    assert struct.unpack("!Q", frame[2:10])[0] == 70000


def test_encode_frame_does_not_mutate_caller_buffer():
    payload = bytearray(b"\x00\x01\x02")
    rt._encode_frame(rt._OP_BINARY, payload, b"\xaa\xbb\xcc\xdd")
    assert payload == bytearray(b"\x00\x01\x02")


def test_frame_header_zero_mask_for_zero_copy_sends():
    # mask bit set + zero key = RFC-legal masking with identity XOR: the payload
    # follows unmodified, so a JPEG memoryview goes out with no copy.
    assert rt._frame_header(rt._OP_BINARY, 6) == bytes([0x82, 0x80 | 6]) + b"\x00" * 4
    h = rt._frame_header(rt._OP_BINARY, 300)
    assert h[1] == 0x80 | 126 and struct.unpack("!H", h[2:4])[0] == 300
    assert h[-4:] == b"\x00" * 4
    h = rt._frame_header(rt._OP_BINARY, 70000)
    assert h[1] == 0x80 | 127 and struct.unpack("!Q", h[2:10])[0] == 70000
    assert h[-4:] == b"\x00" * 4


def test_handshake_request_and_key():
    key = rt._handshake_key(bytes(range(16)))
    assert "\n" not in key
    req = rt._handshake_request("relay.example", "/camera/d1/0?token=t", key)
    assert req.startswith(b"GET /camera/d1/0?token=t HTTP/1.1\r\n")
    assert b"Host: relay.example\r\n" in req
    assert b"Upgrade: websocket\r\n" in req
    assert ("Sec-WebSocket-Key: %s\r\n" % key).encode() in req
    # Cloudflare bot protection rejects default library UAs -- pin ours in place.
    assert b"User-Agent: openmv-cam/1.0\r\n" in req
    assert req.endswith(b"\r\n\r\n")


@pytest.mark.parametrize(("line", "ok"), [
    (b"HTTP/1.1 101 Switching Protocols\r\n", True),
    (b"HTTP/1.0 101 OK\r\n", True),
    (b"HTTP/1.1 403 Forbidden\r\n", False),
    (b"junk\r\n", False),
])
def test_handshake_ok(line, ok):
    assert rt._handshake_ok(line) is ok


# --- throttle ----------------------------------------------------------------------

def test_throttle_caps_rate_and_survives_tick_wrap():
    now = [0]
    t = rt._Throttle(5, lambda: now[0])          # 200 ms interval
    assert t.ready() is True
    assert t.ready() is False                    # same instant: capped
    now[0] += 199
    assert t.ready() is False
    now[0] += 1
    assert t.ready() is True
    # MicroPython ticks wrap at 2^30; the masked diff measures modular elapsed
    # time, so a wrap neither freezes the stream nor bypasses the cap.
    now2 = [0x3FFFFFFF - 50]
    t2 = rt._Throttle(5, lambda: now2[0])
    assert t2.ready() is True
    now2[0] = 10                                 # wrapped: only 61 ticks elapsed
    assert t2.ready() is False                   # still capped -- wrap isn't "forever"
    now2[0] = 149                                # wrapped: exactly 200 ticks elapsed
    assert t2.ready() is True


def test_throttle_zero_fps_means_unlimited():
    t = rt._Throttle(0, lambda: 0)
    assert t.ready() and t.ready() and t.ready()


# --- session state machine -----------------------------------------------------------

def test_session_start_stop():
    s = rt._Session()
    assert s.streaming is False
    assert s.on_text(json.dumps({"type": "start"})) == "start"
    assert s.streaming is True
    assert s.on_text(json.dumps({"type": "stop"})) == "stop"
    assert s.streaming is False


def test_session_tolerates_unknown_and_garbage():
    s = rt._Session()
    assert s.on_text(json.dumps({"type": "hello", "viewers": 3})) == "hello"
    assert s.on_text("not json") is None
    assert s.on_text(json.dumps({"no": "type"})) is None
    assert s.on_text(json.dumps([1, 2])) is None
    assert s.streaming is False


# --- registry + grant plumbing --------------------------------------------------------

def test_stream_registry_reports_names_and_rejects_duplicates():
    rt.Stream("0")
    rt.Stream("tele")
    assert set(rt.streams()) == {"0", "tele"}
    with pytest.raises(ValueError):
        rt.Stream("tele")


def test_grant_per_stream_lookup():
    rt.set_grant({"streams": {"0": {"camera_url": "wss://r/camera/d/0?token=t",
                                    "poll_url": "https://r/poll/d/0?token=t"}},
                  "expires_in_s": 86400})
    assert rt._stream_grant("0")["camera_url"].endswith("/camera/d/0?token=t")
    assert rt._stream_grant("tele") is None      # server didn't grant this name
    rt.set_grant({})                              # falsy -> stored as None
    assert rt._stream_grant("0") is None
    rt.set_grant({"streams": {"0": {}}})
    rt.clear_grant()
    assert rt._stream_grant("0") is None
    rt.set_grant({"expires_in_s": 1})             # malformed: no streams map
    assert rt._stream_grant("0") is None


# --- poll response -------------------------------------------------------------------

def test_parse_poll_response():
    assert rt.parse_poll_response(b'{"watch": true, "viewers": 2}') == (True, 2)
    assert rt.parse_poll_response(b'{"watch": false}') == (False, 0)


# --- module surface --------------------------------------------------------------------

def test_null_log_swallows_everything():
    rt.log.debug("d")
    rt.log.info("i")
    rt.log.warning("w")
    rt.log.error("e")


def test_module_getattr_delegates_constants_to_the_builtin(monkeypatch):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "csi", types.SimpleNamespace(RGB565=1, VGA=2))
    assert rt.RGB565 == 1
    assert rt.VGA == 2


# --- Stream.flush (virtual streams) ----------------------------------------------------

def _bare_stream(name="v1", **kw):
    s = rt.Stream(name, **kw)
    s._task = object()                           # host: no relay task, machinery inert
    s._throttle = rt._Throttle(0, lambda: 0)     # unlimited for tests
    return s


def test_ensure_started_starts_the_machinery_exactly_once():
    s = rt.Stream("once")
    started = []
    s._start = lambda: started.append(1) or setattr(s, "_task", object())
    s._ensure_started()
    s._ensure_started()                          # second call: already running
    assert started == [1]


def test_virtual_stream_flush_encodes_only_while_watched():
    encoded = []
    s = _bare_stream(quality=42, encoder=lambda img, q: encoded.append((img, q)) or b"J")
    assert s.flush("IMG") is False               # not watched: img untouched, nothing queued
    assert encoded == [] and s._frame is None
    s._session.streaming = True
    assert s.flush("IMG") is True
    assert encoded == [("IMG", 42)]
    assert bytes(s._take_frame()) == b"J"        # a view into the stream's own buffer
    assert s._take_frame() is None               # latest-only mailbox drains


def test_virtual_stream_flush_respects_the_fps_cap():
    s = _bare_stream(encoder=lambda img, q: b"J")
    s._session.streaming = True
    now = [0]
    s._throttle = rt._Throttle(5, lambda: now[0])
    assert s.flush("IMG") is True
    assert s.flush("IMG") is False               # 0 ms later: capped
    assert bytes(s._take_frame()) == b"J"


def test_flush_memcpys_into_the_preallocated_buffer_and_drops_while_sending():
    s = _bare_stream(name="pp", encoder=lambda img, q: img)
    s._session.streaming = True

    assert s.flush(b"F1") is True
    v1 = s._take_frame()                         # sender takes it: buffer in flight
    assert s._sending is True
    assert bytes(v1) == b"F1"

    assert s.flush(b"F2") is False               # in flight: dropped, buffer untouched
    assert bytes(v1) == b"F1"                    # the in-flight frame never tore

    s._release_inflight()
    assert s._sending is False
    assert s.flush(b"F3") is True                # send drained: next frame lands
    assert bytes(s._take_frame()) == b"F3"


def test_fit_size_headroom_and_rounding():
    # frame + 1/8th headroom, rounded up to 4 KiB.
    assert rt._fit_size(1) == 4096
    assert rt._fit_size(3641) == 4096            # 3641+455=4096 just fits one block
    assert rt._fit_size(3642) == 8192
    assert rt._fit_size(40000) == 45056          # 40000+5000 -> 11 blocks


def test_dynamic_buffer_allocates_grows_reuses_and_shrinks():
    s = _bare_stream(name="dyn", encoder=lambda img, q: img)
    s._session.streaming = True

    assert s.flush(bytes(5000)) is True          # first frame sizes the buffer
    assert len(s._buf) == rt._fit_size(5000) == 8192
    first = id(s._buf)
    s._take_frame()
    s._release_inflight()

    assert s.flush(bytes(5200)) is True          # jitter within headroom: reuse
    assert id(s._buf) == first
    s._take_frame()
    s._release_inflight()

    assert s.flush(bytes(9000)) is True          # doesn't fit: grow
    assert len(s._buf) == rt._fit_size(9000) == 12288
    s._take_frame()
    s._release_inflight()

    assert s.flush(bytes(500)) is True           # 2x oversized now: shrink
    assert len(s._buf) == rt._fit_size(500) == 4096
    s._take_frame()
    s._release_inflight()

    assert s.flush(bytes(2100)) is True          # above half, below full: reuse
    assert len(s._buf) == 4096


def test_flush_drops_frames_larger_than_bufsize():
    s = _bare_stream(name="tiny", encoder=lambda img, q: img, bufsize=4)
    s._session.streaming = True
    assert s.flush(b"12345") is False            # oversize: dropped, warned once
    assert s.flush(b"123456") is False
    assert s._dropped == 2
    assert s._frame is None
    assert s.flush(b"1234") is True              # exactly bufsize fits
    assert bytes(s._take_frame()) == b"1234"


def test_stream_frame_event_set_and_cleared_around_the_mailbox():
    import types
    s = _bare_stream(encoder=lambda img, q: b"J")
    ev = types.SimpleNamespace(sets=0, clears=0)
    ev.set = lambda: setattr(ev, "sets", ev.sets + 1)
    ev.clear = lambda: setattr(ev, "clears", ev.clears + 1)
    s._frame_event = ev
    s._session.streaming = True
    s.flush("IMG")
    assert ev.sets == 1
    assert bytes(s._take_frame()) == b"J"
    assert ev.clears == 1


# --- the CSI wrapper (fake builtin camera, host asyncio) ------------------------------

class _FakeCam:
    """Stands in for the builtin csi.CSI: returns None (not ready) a few times,
    then frame objects -- exercising the AsyncCSI non-blocking poll loop."""

    def __init__(self, not_ready=2):
        self._not_ready = not_ready
        self._n = 0
        self.resets = 0

    def reset(self):
        self.resets += 1

    def snapshot(self, blocking=True, **kwargs):
        assert blocking is False                 # the wrapper must never block
        if self._not_ready > 0:
            self._not_ready -= 1
            return None
        self._n += 1
        return "IMG%d" % self._n


def _wrapper(stream="cam-test", **kw):
    cam = _FakeCam()
    w = rt.CSI(cam=cam, stream=stream, **kw)
    w._stream._task = object()                   # host: machinery inert
    w._stream._throttle = rt._Throttle(0, lambda: 0)
    return w, cam


def _shim_sleep_ms():
    if not hasattr(asyncio, "sleep_ms"):         # host asyncio: MicroPython shim
        asyncio.sleep_ms = lambda ms: asyncio.sleep(ms / 1000)


def test_wrapper_delegates_to_the_builtin_camera():
    w, cam = _wrapper()
    w.reset()
    assert cam.resets == 1


@pytest.mark.parametrize(("args", "kwargs", "want"), [
    ((), {}, "0"),                               # builtin default cid (-1) -> "0"
    ((2,), {}, "2"),                             # positional cid names the stream
    ((), {"cid": 1}, "1"),
    ((), {"stream": "front"}, "front"),          # explicit name wins
])
def test_stream_name_derivation(args, kwargs, want):
    w = rt.CSI(*args, cam=_FakeCam(), **kwargs)
    assert w.stream.name == want


def test_frames_encode_at_disposal_time_not_at_capture():
    _shim_sleep_ms()
    seen = []
    w, _cam = _wrapper(encoder=lambda img, q: seen.append(img) or b"J")
    w._stream._session.streaming = True
    img1 = asyncio.run(w.snapshot())
    assert img1 == "IMG1"
    assert seen == []                            # the app still owns IMG1: untouched
    img2 = asyncio.run(w.snapshot())             # entry disposes IMG1 into the stream
    assert img2 == "IMG2"
    assert seen == ["IMG1"]
    assert bytes(w._stream._take_frame()) == b"J"


def test_manual_flush_disposes_the_pending_frame():
    _shim_sleep_ms()
    seen = []
    w, _cam = _wrapper(encoder=lambda img, q: seen.append(img) or b"J")
    w._stream._session.streaming = True
    asyncio.run(w.snapshot())
    assert w.flush() is True                     # before deep sleep: don't lose the frame
    assert seen == ["IMG1"]
    assert w.flush() is False                    # nothing pending now


def test_flush_with_nobody_watching_discards_quietly():
    _shim_sleep_ms()
    seen = []
    w, _cam = _wrapper(encoder=lambda img, q: seen.append(img) or b"J")
    asyncio.run(w.snapshot())
    assert w.flush() is False                    # unwatched: no encode, frame just dropped
    assert seen == []


def test_live_active_mirrors_the_stream_session():
    w, _cam = _wrapper()
    assert w.live_active is False
    w._stream._session.streaming = True
    assert w.live_active is True


# --- the OTA check-in extension handlers ---------------------------------------------------

def test_on_checkin_sets_the_live_grant():
    rt.set_grant(None)
    rt._on_checkin({"live": {"streams": {"0": {"camera_url": "wss://r/camera/d/0?token=t"}}}})
    assert rt._stream_grant("0")["camera_url"].endswith("token=t")
    rt._on_checkin({"update": False})                # no live key -> grant cleared
    assert rt._stream_grant("0") is None


def test_contribute_reports_the_stream_names():
    rt.Stream("0")
    rt.Stream("tele")
    assert set(rt._contribute()["streams"]) == {"0", "tele"}

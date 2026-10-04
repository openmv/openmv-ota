"""The board's cloud level, end to end on the host: boards.json -> the firmware stamp
(``_ota_config.CLOUD`` / ``LIVE_FRAMESIZE``) -> ``openmv_ota.cloud_level()`` -> what the
cloud SDK (csi, logs, datalog) starts. A camera never starts what its level leaves out:
"no-live" builds no relay stream and "ota-only" no datalake sinks either, and neither
allocates for the feature it does not have. Also the Live frame-size cap a "full" board
downscales to before encoding."""

from __future__ import annotations

import asyncio
import logging
import sys
import types

import pytest

from openmv_ota.build.device import openmv_ota as rt
from openmv_ota.build.device.openmv_cloud import _lib
from openmv_ota.build.device.openmv_cloud import csi
from openmv_ota.build.device.openmv_cloud import datalog as dl
from openmv_ota.build.device.openmv_cloud import logs as lg


def _firmware(monkeypatch, **stamp):
    """The device runtime beside the SDK, over a frozen ``_ota_config`` carrying ``stamp``
    (``None`` = no _ota_config module at all)."""
    monkeypatch.setitem(sys.modules, "openmv_ota", rt)
    monkeypatch.setitem(sys.modules, "_ota_config",
                        None if stamp.get("absent") else types.SimpleNamespace(**stamp))
    _lib._lvl, csi._cap = None, 0


@pytest.fixture(autouse=True)
def _clean():
    csi._streams.clear()
    csi.set_grant(None)
    dl._topics.clear()
    yield
    csi._streams.clear()
    csi.set_grant(None)
    dl._topics.clear()


# --- the build stamp ------------------------------------------------------------------

@pytest.mark.parametrize(("board", "want"), [
    ("OPENMV_N6", ("full", "QVGA")),
    ("OPENMV4", ("full", "QVGA")),
    ("OPENMV3", ("no-live", None)),      # placeholder until the bench says otherwise
    ("OPENMV2", (None, None)),           # no level: the M4's runtime has no network stack
    ("OPENMVPT", (None, None)),
])
def test_the_build_stamps_each_boards_level_and_live_cap(board, want):
    from openmv_ota.build.firmware import _cloud_stamp
    assert _cloud_stamp(board) == want


def test_ota_config_carries_the_stamp(make_project, monkeypatch):
    """Through `build firmware`: the generated _ota_config says CLOUD and LIVE_FRAMESIZE,
    beside TLS_VERIFY."""
    from openmv_ota.build import firmware as fw

    from .test_firmware import _fake_make
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    monkeypatch.setattr(fw, "_ensure_mpy_cross", lambda repo: None)
    root, repo, _app = make_project(boards=("OPENMV4",), ota=True)
    r = fw.build_firmware(root, firmware=repo, boards=["OPENMV4"], keep_build_dir=True)[0]
    ns: dict = {}
    exec((r.build_dir / "_ota_config.py").read_text(), ns)
    assert ns["CLOUD"] == "full" and ns["LIVE_FRAMESIZE"] == "QVGA"


# --- the helper ------------------------------------------------------------------------

@pytest.mark.parametrize(("stamp", "want"), [
    ({"absent": True}, "full"),          # no _ota_config: a non-OTA firmware
    ({}, "full"),                        # firmware built before the stamp
    ({"CLOUD": None}, "full"),           # a board with no level
    ({"CLOUD": "full"}, "full"),
    ({"CLOUD": "no-live"}, "no-live"),
    ({"CLOUD": "ota-only"}, "ota-only"),
    ({"CLOUD": "bogus"}, "full"),        # never cut a feature on a value it doesn't know
])
def test_cloud_level_reads_the_stamp_defensively(monkeypatch, stamp, want):
    _firmware(monkeypatch, **stamp)
    assert rt.cloud_level() == want
    assert _lib._level() == want


def test_the_sdk_reads_the_level_once(monkeypatch):
    _firmware(monkeypatch, CLOUD="no-live")
    assert _lib._level() == "no-live"
    sys.modules["_ota_config"].CLOUD = "ota-only"     # the firmware cannot change under us
    assert _lib._level() == "no-live"


@pytest.mark.parametrize("runtime", [None, types.SimpleNamespace()])
def test_the_sdk_without_a_runtime_that_knows_does_everything(monkeypatch, runtime):
    """No openmv_ota beside the SDK (ImportError), or one predating cloud_level()
    (AttributeError): "full", as the SDK always behaved."""
    monkeypatch.setitem(sys.modules, "openmv_ota", runtime)
    assert _lib._level() == "full"


def test_the_checkin_reports_the_level(monkeypatch):
    _firmware(monkeypatch, CLOUD="ota-only")
    assert rt._checkin_body({}, {})["cloud_level"] == "ota-only"


# --- csi without Live -------------------------------------------------------------------

class _Cam:
    def __init__(self):
        self.n = 0

    def snapshot(self, blocking=True, **kw):
        self.n += 1
        return "IMG%d" % self.n


def test_no_live_stream_is_inert(monkeypatch):
    _firmware(monkeypatch, CLOUD="no-live")
    s = csi.Stream("overlay", bufsize=64 * 1024)
    assert csi.streams() == [] and csi._contribute() == {"streams": []}
    assert s._buf is None                             # not even a fixed bufsize= is allocated
    s._start = lambda: pytest.fail("no relay task without Live")
    s._ensure_started()
    s._session.streaming = True                       # even if something claimed a viewer
    assert s.flush("IMG") is False and s._frame is None
    csi.Stream("overlay")                             # unregistered: the name stays free


def test_no_live_csi_is_the_plain_camera(monkeypatch):
    if not hasattr(asyncio, "sleep_ms"):
        asyncio.sleep_ms = lambda ms: asyncio.sleep(ms / 1000)
    _firmware(monkeypatch, CLOUD="ota-only")
    seen = []
    cam = csi.CSI(cam=_Cam(), encoder=lambda img, q: seen.append(img) or b"J")
    cam._stream._start = lambda: pytest.fail("no relay task without Live")
    assert asyncio.run(cam.snapshot()) == "IMG1"
    assert asyncio.run(cam.snapshot()) == "IMG2"
    assert cam._pending is None and seen == []        # nothing held back, nothing encoded
    assert cam.flush() is False and cam.live_active is False
    assert csi.streams() == []


def test_no_live_keeps_no_grant(monkeypatch):
    _firmware(monkeypatch, CLOUD="no-live")
    csi._on_checkin({"live": {"streams": {"0": {"camera_url": "wss://r/c/d/0?token=t"}}}})
    assert csi._grant is None


def test_full_still_streams(monkeypatch):
    _firmware(monkeypatch, CLOUD="full")
    s = csi.Stream("0")
    assert csi.streams() == ["0"] and not s._off
    csi._on_checkin({"live": {"streams": {"0": {"camera_url": "u"}}}})
    assert csi._stream_grant("0") == {"camera_url": "u"}


# --- the Live frame-size cap -----------------------------------------------------------

def test_the_device_framesize_table_matches_the_registry():
    from openmv_ota.romfs.boards import LIVE_FRAMESIZES
    assert csi._FRAMESIZES == LIVE_FRAMESIZES


@pytest.mark.parametrize(("w", "h", "cap", "want"), [
    (320, 240, (160, 120), 0.5),         # QVGA app, QQVGA Live
    (640, 480, (160, 120), 0.25),
    (640, 360, (160, 120), 0.25),        # 16:9 keeps its aspect: limited by width
    (240, 240, (160, 120), 0.5),         # square: limited by height
    (160, 120, (160, 120), None),        # already the cap
    (80, 60, (160, 120), None),          # smaller: never upscaled
    (320, 240, None, None),              # no cap
    (0, 240, (160, 120), None),          # nonsense dimensions: leave it alone
])
def test_scale_for(w, h, cap, want):
    assert csi._scale_for(w, h, cap) == want


@pytest.mark.parametrize(("stamp", "want"), [
    ({"absent": True}, None),
    ({}, None),
    ({"LIVE_FRAMESIZE": None}, None),
    ({"LIVE_FRAMESIZE": "HD"}, None),     # a name this SDK doesn't know: no cap
    ({"LIVE_FRAMESIZE": "QQVGA"}, (160, 120)),
])
def test_live_cap_reads_the_stamp_once(monkeypatch, stamp, want):
    _firmware(monkeypatch, **stamp)
    assert csi._live_cap() == want
    sys.modules["_ota_config"] = types.SimpleNamespace(LIVE_FRAMESIZE="VGA")
    assert csi._live_cap() == want                    # cached


class _Img:
    """The OpenMV image API the encoder touches: in-place to_jpeg, aliasing bytearray()."""

    def __init__(self, w, h, fail=None):
        self.w, self.h, self.fail, self.calls = w, h, fail, []
        self.buf = bytearray(b"JPEG")

    def width(self):
        return self.w

    def height(self):
        return self.h

    def to_jpeg(self, **kw):
        self.calls.append(kw)
        if self.fail and "x_scale" in kw:
            raise self.fail
        return self

    def bytearray(self):
        return self.buf


def test_encoder_downscales_in_place_to_the_cap(monkeypatch):
    _firmware(monkeypatch, LIVE_FRAMESIZE="QQVGA")
    img = _Img(320, 240)
    view = csi._default_encoder(img, 50)
    assert img.calls == [{"quality": 50, "x_scale": 0.5, "y_scale": 0.5}]
    assert isinstance(view, memoryview) and view.obj is img.buf   # zero-copy alias


def test_encoder_leaves_a_frame_within_the_cap_alone(monkeypatch):
    _firmware(monkeypatch, LIVE_FRAMESIZE="QVGA")
    img = _Img(320, 240)
    csi._default_encoder(img, 50)
    assert img.calls == [{"quality": 50}]


def test_encoder_falls_back_unscaled_once_scaling_fails(monkeypatch):
    """The scaled intermediate comes from the firmware's frame-buffer allocator; when it is
    full the frame still goes out (unscaled), and scaling stays off rather than failing
    every frame."""
    _firmware(monkeypatch, LIVE_FRAMESIZE="QQVGA")
    img = _Img(320, 240, fail=MemoryError("Out of memory"))
    assert bytes(csi._default_encoder(img, 50)) == b"JPEG"
    assert img.calls == [{"quality": 50, "x_scale": 0.5, "y_scale": 0.5}, {"quality": 50}]
    again = _Img(320, 240, fail=MemoryError())
    csi._default_encoder(again, 50)
    assert again.calls == [{"quality": 50}]


# --- logs / datalog at ota-only ----------------------------------------------------------

def test_logs_enable_at_ota_only_allocates_nothing(monkeypatch):
    _firmware(monkeypatch, CLOUD="ota-only")
    monkeypatch.setattr(lg, "_enable", lambda *a: pytest.fail("ota-only must not enable"))
    members = list(_lib.budget._members)
    root = logging.getLogger()
    handlers = list(root.handlers)
    assert lg.enable() is None
    assert lg._outbox is None and lg._kick is None
    assert _lib.budget._members == members and root.handlers == handlers
    assert csi.streams() == []


@pytest.mark.parametrize(("level", "live"), [("full", True), ("no-live", False)])
def test_logs_enable_passes_live_only_on_full(monkeypatch, level, live):
    _firmware(monkeypatch, CLOUD=level)
    got = []
    monkeypatch.setattr(lg, "_enable", lambda *a: got.append(a) or "handler")
    assert lg.enable(spool_path="/sd") == "handler"
    assert got == [(live, logging.INFO, None, None, 5, "/sd", False)]


def test_no_live_console_keeps_no_ring():
    c = lg._Console(sid="s", live=False)
    assert [c.add("a\n", False), c.add("b\n", True)] == [0, 1]   # seq still counts
    assert c._ring == [] and c._pending == [] and c._ring_size == 0


def test_datalog_at_ota_only_drops_cheaply(monkeypatch):
    _firmware(monkeypatch, CLOUD="ota-only")
    members = list(_lib.budget._members)
    assert dl.post("imu", {"ax": 1}) is False
    assert dl._topics == {} and _lib.budget._members == members


def test_datalog_enable_at_ota_only_starts_nothing(monkeypatch):
    _firmware(monkeypatch, CLOUD="ota-only")
    monkeypatch.setattr(dl, "_start", lambda: pytest.fail("ota-only must not start a flusher"))
    monkeypatch.setattr(dl, "_spool_path", None)
    dl.enable(spool_path="/sd")
    assert dl._spool_path is None


@pytest.mark.parametrize("level", ["full", "no-live"])
def test_datalog_runs_wherever_there_is_a_datalake(monkeypatch, level):
    _firmware(monkeypatch, CLOUD=level)
    started = []
    monkeypatch.setattr(dl, "_start", lambda: started.append(1))
    monkeypatch.setattr(dl, "_spool_path", None)
    monkeypatch.setattr(dl, "_write_through", False)
    dl.enable(spool_path=None, write_through=True)
    assert started == [1] and dl._write_through is True
    assert dl.post("imu", {"ax": 1}) is True
    dl._topics["imu"]["box"]._budget.leave(dl._topics["imu"]["box"])

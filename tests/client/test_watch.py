"""`client device watch`: the relay viewer loop, its CLI, and one real WebSocket round trip."""

from __future__ import annotations

import json
import threading
from http import HTTPStatus

import pytest
from websockets.exceptions import ConnectionClosedOK, InvalidMessage, InvalidStatus
from websockets.sync.server import serve

from openmv_ota.cli import main
from openmv_ota.client import cli as client_cli
from openmv_ota.client import watch
from openmv_ota.client.errors import ClientError

URL = "wss://relay.test/watch/d1/%s?token=tok"


def _grant(*streams, token="tok"):
    return {"token": token, "expires_in_s": 300,
            "streams": {s: {"watch_url": URL % s} for s in streams}}


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _wait(clock):
    """A recv that times out: the clock moves on by the timeout it was given."""
    def recv(timeout):
        clock.t += timeout
        raise TimeoutError
    return recv


class FakeWs:
    def __init__(self, *script):
        self.script = list(script)
        self.timeouts = []
        self.closed = False

    def recv(self, timeout=None):
        self.timeouts.append(timeout)
        if not self.script:
            raise ConnectionClosedOK(None, None)
        item = self.script.pop(0)
        if callable(item):
            return item(timeout)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


def _hello(online=True, viewers=1):
    return json.dumps({"type": "hello", "cameraOnline": online, "viewers": viewers})


def _batch(sid, seq, text, **kw):
    return json.dumps(dict(kw, sid=sid, seq=seq, text=text)).encode()


def _run(sockets, grant=None, clock=None, **kw):
    """watch() over a queue of fake sockets (or exceptions to raise on open)."""
    clock = clock or Clock()
    events, opened, mints = [], [], []
    queue = list(sockets)

    def mint():
        mints.append(1)
        return grant or _grant("0", "console")

    def open_ws(url, token):
        opened.append((url, token))
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    out = watch.watch(mint, open_ws, lambda k, i: events.append((k, i)),
                      now=clock.now, sleep=clock.sleep, **kw)
    return out, events, opened, mints, clock


# --- pick_stream ------------------------------------------------------------

def test_pick_stream_defaults_to_the_first_video_stream_and_strips_the_token():
    assert watch.pick_stream(_grant("console", "1", "0"), None, False) == (
        "1", "wss://relay.test/watch/d1/1", "tok")


def test_pick_stream_console_and_named():
    assert watch.pick_stream(_grant("0", "console"), "0", True)[0] == "console"
    assert watch.pick_stream(_grant("0", "1"), "1", False)[0] == "1"


@pytest.mark.parametrize("grant, stream, console, said", [
    (_grant("0", token=""), None, False, "no live relay"),
    ({"token": "tok", "streams": {}}, None, False, "no live relay"),
    (_grant("console"), None, False, "no video stream (streams: console)"),
    (_grant("0"), "9", False, "no stream '9' on this device (streams: 0)"),
    (_grant("0"), None, True, "no stream 'console'"),
])
def test_pick_stream_says_what_is_missing(grant, stream, console, said):
    with pytest.raises(ClientError, match=said.replace("(", r"\(").replace(")", r"\)")) as e:
        watch.pick_stream(grant, stream, console)
    assert e.value.exit_code == 1


# --- ConsoleDedup -----------------------------------------------------------

def test_dedup_drops_the_replayed_backlog_and_keeps_the_new_lines():
    d = watch.ConsoleDedup()
    assert d.fresh({"sid": "a", "seq": 1, "text": "one\ntwo\n"}) == "one\ntwo\n"
    assert d.fresh({"sid": "a", "seq": 1, "text": "one\ntwo\n"}) == ""
    assert d.fresh({"sid": "a", "seq": 2, "text": "two\nthree\n"}) == "three\n"
    assert d.fresh({"sid": "b", "seq": 1, "text": "after reboot\n"}) == "after reboot\n"


def test_dedup_passes_unnumbered_and_empty_batches():
    d = watch.ConsoleDedup()
    assert d.fresh({"text": "no seq\n"}) == "no seq\n"
    assert d.fresh({"sid": "a", "seq": 5, "text": ""}) == ""
    assert d.fresh({"sid": "a", "seq": 1, "text": "x\n"}) == "x\n"   # the empty batch set no floor


def test_dedup_forgets_all_but_the_newest_sessions():
    d = watch.ConsoleDedup()
    for i in range(watch._SIDS_KEPT + 1):
        d.fresh({"sid": "s%d" % i, "seq": 1, "text": "x\n"})
    assert d.fresh({"sid": "s0", "seq": 1, "text": "x\n"}) == "x\n"       # evicted: shown again
    assert d.fresh({"sid": "s%d" % watch._SIDS_KEPT, "seq": 1, "text": "x\n"}) == ""


# --- watch() ----------------------------------------------------------------

def test_a_snapshot_is_one_frame_and_closes_the_socket():
    ws = FakeWs(_hello(viewers=2), b"\xff\xd8jpeg")
    out, events, opened, _, _ = _run([ws])
    assert out == {"frames": 1, "batches": 0, "seconds": 0.0}
    assert events == [("hello", {"online": True, "viewers": 2}),
                      ("frame", {"n": 1, "stream": "0", "data": b"\xff\xd8jpeg"})]
    assert opened == [("wss://relay.test/watch/d1/0", "tok")]
    assert ws.closed and ws.timeouts == [60.0, 60.0]


def test_seconds_bounds_an_unlimited_watch():
    clock = Clock()
    ws = FakeWs(b"f1", b"f2", _wait(clock))
    out, events, *_ = _run([ws], clock=clock, frames=0, seconds=5)
    assert out == {"frames": 2, "batches": 0, "seconds": 5.0}
    assert ws.timeouts[-1] == 5.0                      # the deadline, sooner than idle


def test_idle_gives_up_on_a_silent_camera():
    clock = Clock()
    ws = FakeWs(_hello(online=False), json.dumps({"type": "camera-offline"}), _wait(clock))
    with pytest.raises(ClientError, match="nothing from the camera for 60 s") as e:
        _run([ws], clock=clock)
    assert e.value.exit_code == 1 and ws.closed


def test_relay_state_that_is_not_understood_is_ignored():
    ws = FakeWs("not json", "[1]", json.dumps({"type": "who-knows"}),
                json.dumps({"type": "camera-offline"}), b"f")
    _, events, *_ = _run([ws])
    assert [k for k, _ in events] == ["offline", "frame"]


def test_no_bounds_waits_in_backoff_sized_slices():
    ws = FakeWs(_batch("a", 1, "x\n"), KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _run([ws], console=True, idle=0)
    assert ws.timeouts == [watch._BACKOFF_MAX_S] * 2


def test_console_reconnects_with_a_fresh_grant_and_shows_each_line_once():
    clock = Clock()
    first = FakeWs(_batch("a", 1, "one\ntwo\n", ts=1.5))       # then the relay drops it
    second = FakeWs(_batch("a", 1, "one\ntwo\n"), _batch("a", 3, "three\n"),
                    b"\xff not json", b"[1]", _wait(clock))
    out, events, opened, mints, clock = _run([first, second], clock=clock, console=True,
                                             seconds=10, idle=0)
    assert [(k, i.get("text"), i.get("after_s")) for k, i in events] == [
        ("console", "one\ntwo\n", None), ("reconnect", None, 1), ("console", "three\n", None)]
    assert events[0][1] == {"sid": "a", "seq": 1, "text": "one\ntwo\n", "ts": 1.5}
    assert len(mints) == 2 and len(opened) == 2 and first.closed and second.closed
    assert out["batches"] == 2 and out["frames"] == 0


def test_an_unreachable_relay_is_retried_with_backoff():
    clock = Clock()
    out, events, opened, mints, clock = _run(
        [OSError("refused"), TimeoutError(), InvalidMessage("cut short"), FakeWs(b"f")],
        clock=clock)
    assert out["frames"] == 1 and len(mints) == 4
    assert clock.slept == [1, 2, 4]
    assert [i["after_s"] for k, i in events if k == "reconnect"] == [1, 2, 4]


def test_reconnecting_stops_at_the_deadline():
    clock = Clock()
    out, events, *_ = _run([OSError(), OSError()], clock=clock, frames=0, seconds=1.5)
    assert clock.slept == [1, 0.5] and out["seconds"] == 1.5


def test_a_drop_at_the_deadline_ends_the_watch():
    clock = Clock()

    def drop(timeout):
        clock.t += timeout
        raise ConnectionClosedOK(None, None)

    out, events, opened, *_ = _run([FakeWs(drop)], clock=clock, frames=0, seconds=3)
    assert out["seconds"] == 3.0 and len(opened) == 1 and not events


def test_reconnecting_gives_up_when_idle():
    with pytest.raises(ClientError, match="nothing from the camera for 2 s"):
        _run([OSError(), OSError()], idle=2)


def _status(code):
    from websockets.http11 import Response
    from websockets.datastructures import Headers
    return InvalidStatus(Response(code, "x", Headers()))


@pytest.mark.parametrize("code, said", [
    (403, r"HTTP 403: a refused token, or a blocked client\)"), (404, r"HTTP 404\)")])
def test_a_refused_upgrade_is_final(code, said):
    with pytest.raises(ClientError, match="the relay refused to open 0 " r"\(" + said) as e:
        _run([_status(code)])
    assert e.value.exit_code == 1


# --- the CLI ----------------------------------------------------------------

class FakeApi:
    def __init__(self, grant):
        self.grant = grant

    def viewer_grant(self, device_id):
        assert device_id == "d1"
        return self.grant


@pytest.fixture
def cli(monkeypatch):
    """main() with the API and the relay faked; returns a function that queues sockets."""
    monkeypatch.setenv("OPENMV_OTA_SERVER", "https://ota.test")
    monkeypatch.setenv("OPENMV_OTA_TOKEN", "tok")
    state = {"grant": _grant("0", "console"), "sockets": [], "agents": []}
    monkeypatch.setattr(client_cli, "_make_api", lambda cfg: FakeApi(state["grant"]))

    def connect(url, token, agent):
        state["agents"].append(agent)
        return state["sockets"].pop(0)

    monkeypatch.setattr(watch, "connect", connect)
    return state


def test_cli_snapshot_saves_the_frame(cli, tmp_path, capsys):
    cli["sockets"].append(FakeWs(_hello(), b"\xff\xd8one"))
    latest = tmp_path / "now.jpg"
    assert main(["client", "device", "watch", "--device-id", "d1", "--out", str(tmp_path / "f"),
                 "--latest", str(latest)]) == 0
    assert (tmp_path / "f" / "0-000001.jpg").read_bytes() == b"\xff\xd8one"
    assert latest.read_bytes() == b"\xff\xd8one" and not latest.with_name("now.jpg.tmp").exists()
    io = capsys.readouterr()
    assert io.out.startswith("frame 1  0.0 KiB  -> ")
    assert "camera online (1 viewer)" in io.err and "1 frame(s) in " in io.err
    from openmv_ota import __version__
    assert cli["agents"] == ["openmv-ota/%s" % __version__]


def test_cli_json_is_one_object_per_event(cli, capsys):
    cli["sockets"].append(FakeWs(_hello(online=False), json.dumps({"type": "camera-offline"}),
                                 b"ab", b"cd"))
    assert main(["client", "device", "watch", "--device-id", "d1", "--frames", "2",
                 "--json"]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [x["event"] for x in lines] == ["hello", "offline", "frame", "frame", "done"]
    assert lines[2] == {"event": "frame", "n": 1, "stream": "0", "bytes": 2, "saved": []}
    assert lines[-1]["frames"] == 2


def test_cli_console_prints_the_text_as_is(cli, capsys):
    clock_free = FakeWs(_batch("a", 1, "hello\n"), _batch("a", 2, "world\n"), KeyboardInterrupt())
    cli["sockets"].append(clock_free)
    assert main(["client", "device", "watch", "--device-id", "d1", "--console"]) == 0
    io = capsys.readouterr()
    assert io.out == "hello\nworld\n"
    assert "2 console batch(es)" in io.err


def test_cli_ctrl_c_on_video_reports_what_it_got(cli, capsys):
    cli["sockets"].append(FakeWs(b"f", KeyboardInterrupt()))
    cli["sockets"][0].script.insert(0, _hello(online=False, viewers=2))
    assert main(["client", "device", "watch", "--device-id", "d1", "--frames", "0"]) == 0
    err = capsys.readouterr().err
    assert "camera offline (2 viewers); waiting for it" in err and err.endswith("1 frame(s)\n")


def test_cli_says_when_the_relay_drops(cli, monkeypatch, capsys):
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)
    cli["sockets"] += [FakeWs(json.dumps({"type": "camera-offline"})), FakeWs(b"f")]
    assert main(["client", "device", "watch", "--device-id", "d1"]) == 0
    err = capsys.readouterr().err
    assert "camera went offline; waiting" in err
    assert "relay connection lost; reconnecting in 1 s" in err


def test_cli_console_takes_no_frame_options(cli, capsys):
    assert main(["client", "device", "watch", "--device-id", "d1", "--console",
                 "--frames", "3"]) == 2
    assert "are for video" in capsys.readouterr().err


def test_cli_without_a_relay_fails_cleanly(cli, capsys):
    cli["grant"] = {"token": "", "streams": {}}
    assert main(["client", "device", "watch", "--device-id", "d1"]) == 1
    assert "no live relay configured" in capsys.readouterr().err


# --- a real WebSocket -------------------------------------------------------

@pytest.fixture
def relay():
    """A local stand-in for the relay's /watch endpoint; ``refuse`` answers the upgrade 403."""
    seen, opts = {}, {"refuse": False}

    def process_request(conn, request):
        seen.update(path=request.path, auth=request.headers.get("Authorization"),
                    agent=request.headers.get("User-Agent"))
        if opts["refuse"]:
            return conn.respond(HTTPStatus.FORBIDDEN, "no\n")
        return None

    def handler(conn):
        conn.send(_hello())
        conn.send(b"\xff\xd8frame")
        try:
            conn.recv(timeout=5)
        except Exception:   # noqa: BLE001 -- the client hanging up is the normal end
            pass

    with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        port = server.socket.getsockname()[1]
        grant = {"token": "tok", "streams": {
            "0": {"watch_url": "ws://127.0.0.1:%d/watch/d1/0?token=tok" % port}}}
        yield grant, seen, opts
        server.shutdown()


def test_connect_sends_the_token_as_a_header_with_our_agent(relay):
    grant, seen, _ = relay
    events = []
    out = watch.watch(lambda: grant, lambda u, t: watch.connect(u, t, "openmv-ota/test"),
                      lambda k, i: events.append((k, i)))
    assert out["frames"] == 1
    assert events[-1][1]["data"] == b"\xff\xd8frame"
    assert seen == {"path": "/watch/d1/0", "auth": "Bearer tok", "agent": "openmv-ota/test"}


def test_connect_surfaces_a_real_refusal(relay):
    grant, _, opts = relay
    opts["refuse"] = True
    with pytest.raises(ClientError, match="HTTP 403"):
        watch.watch(lambda: grant, lambda u, t: watch.connect(u, t, "openmv-ota/test"),
                    lambda k, i: None)

"""`client device watch`: the dashboard's Live view from a terminal.

A viewer grant (what `client device grant` prints) names one relay WebSocket per stream. A
video stream carries the camera's frames as binary JPEG messages; the ``console`` stream carries
its log as binary JSON batches ``{sid, seq, text, ts}``; text messages on either are the relay's
own state (``hello`` with ``cameraOnline``, ``camera-offline``). The relay checks the token only
when the socket opens, so a watch that outlives the grant mints a fresh one on every reconnect.

Watching is not free for the camera: while a viewer is connected the relay tells it someone is
watching and it uploads frames, exactly as for an open dashboard tab. So a video watch is
BOUNDED by default -- one frame (a snapshot) unless ``--frames``/``--seconds`` say otherwise --
and gives up when nothing arrives for ``idle`` seconds rather than waiting on an offline camera
forever."""

from __future__ import annotations

import json
import time

from .errors import ClientError

CONSOLE = "console"
_MAX_FRAME = 16 * 1024 * 1024   # one message's ceiling; a VGA JPEG is tens of KB
_BACKOFF_MAX_S = 30             # reconnect waits double from 1 s up to this, as the website's do
_SIDS_KEPT = 8                  # boot sessions remembered for de-duplicating the console replay


def pick_stream(grant: dict, stream: str | None, console: bool) -> tuple[str, str, str]:
    """``(name, url, token)`` for the stream to watch, from a viewer grant. The URL loses its
    ``?token=``: the token goes in the Authorization header, never in a URL that lands in logs.
    With no ``stream`` named, video means the grant's first non-console stream."""
    streams = grant.get("streams") or {}
    token = grant.get("token") or ""
    if not token or not streams:
        raise ClientError("the server has no live relay configured: nothing to watch", exit_code=1)
    if console:
        stream = CONSOLE
    elif stream is None:
        video = [s for s in streams if s != CONSOLE]
        if not video:
            raise ClientError("the device has no video stream (streams: %s)"
                              % ", ".join(streams), exit_code=1)
        stream = video[0]
    if stream not in streams:
        raise ClientError("no stream %r on this device (streams: %s)"
                          % (stream, ", ".join(streams)), exit_code=1)
    url = str((streams[stream] or {}).get("watch_url") or "").split("?", 1)[0]
    return stream, url, token


class ConsoleDedup:
    """Each console line printed once. The relay replays its recent backlog to every new viewer,
    so a reconnect re-sends lines already shown; a batch's ``text`` is one or more lines numbered
    from ``seq`` within boot session ``sid``, and anything at or below the highest seq already
    seen for that sid is dropped. The last few sessions are remembered, which covers a backlog
    that spans a reboot."""

    def __init__(self):
        self._top: dict = {}

    def fresh(self, batch: dict) -> str:
        sid, seq, text = batch.get("sid"), batch.get("seq"), batch.get("text") or ""
        if not isinstance(seq, int):
            return text                      # unnumbered: nothing to compare, show it
        top = self._top.get(sid, -1)
        lines = text.splitlines(keepends=True)
        out = "".join(line for i, line in enumerate(lines) if seq + i > top)
        if lines:
            self._top.pop(sid, None)         # re-insert: most recently seen session last
            self._top[sid] = max(top, seq + len(lines) - 1)
            while len(self._top) > _SIDS_KEPT:
                del self._top[next(iter(self._top))]
        return out


def connect(url: str, token: str, user_agent: str):
    """A sync WebSocket to the relay. A real User-Agent is not optional: Cloudflare's bot
    protection answers default library agents with 403 before the relay ever runs."""
    from websockets.sync.client import connect as ws_connect
    return ws_connect(url, additional_headers={"Authorization": "Bearer " + token},
                      user_agent_header=user_agent, max_size=_MAX_FRAME, open_timeout=15)


def _refused(e) -> int | None:
    """The HTTP status a relay refused the upgrade with, else None (a network failure)."""
    resp = getattr(e, "response", None)
    return getattr(resp, "status_code", None)


def watch(mint, open_ws, on_event, *, stream=None, console=False, frames=1, seconds=None,
          idle=60.0, now=time.monotonic, sleep=time.sleep) -> dict:
    """Watch one stream until a bound is reached; returns ``{"frames", "batches", "seconds"}``.

    ``mint()`` returns a fresh viewer grant, ``open_ws(url, token)`` a connection with
    ``recv(timeout)``/``close()``, and ``on_event(kind, info)`` receives ``hello``
    (``online``, ``viewers``), ``offline``, ``reconnect`` (``after_s``), ``frame`` (``n``,
    ``stream``, ``data``) and ``console`` (the batch, its ``text`` already de-duplicated). Bounds: ``frames`` (video; 0 = no limit), ``seconds``
    (None = no limit), and ``idle`` -- seconds with no data before giving up with a ClientError
    (0 = wait forever). A refused upgrade (403/404/...) is final; a dropped or unreachable
    socket is retried with a fresh grant, backing off to ``_BACKOFF_MAX_S``."""
    from websockets.exceptions import ConnectionClosed, WebSocketException

    start = now()
    deadline = start + seconds if seconds else None
    last = start                                   # the last DATA (frame or console batch)
    got = batches = tries = 0
    dedup = ConsoleDedup()

    def result():
        return {"frames": got, "batches": batches, "seconds": round(now() - start, 3)}

    def budget():
        """Seconds the next wait may take, or None when a bound is already reached."""
        waits = []
        if deadline is not None:
            waits.append(deadline - now())
        if idle:
            waits.append(last + idle - now())
        if not waits:
            return _BACKOFF_MAX_S
        left = min(waits)
        if left > 0:
            return left
        if deadline is not None and now() >= deadline:
            return None
        raise ClientError("nothing from the camera for %d s: it is offline or not streaming"
                          % idle, exit_code=1)

    while True:
        if budget() is None:                       # a backoff can end exactly on the deadline
            return result()
        name, url, token = pick_stream(mint(), stream, console)
        try:
            ws = open_ws(url, token)
        except (OSError, TimeoutError):
            ws = None                              # unreachable: retry
        except WebSocketException as e:
            code = _refused(e)
            if code is not None:                   # an answer, not a failure: retrying won't change it
                raise ClientError("the relay refused to open %s (HTTP %d%s)" % (
                    name, code, ": a refused token, or a blocked client" if code == 403 else ""),
                    exit_code=1) from None
            ws = None                              # a handshake cut short: retry
        if ws is not None:
            tries = 0
            try:
                while True:
                    wait = budget()
                    if wait is None:
                        return result()
                    try:
                        msg = ws.recv(timeout=wait)
                    except TimeoutError:
                        continue                   # re-check the bounds
                    except ConnectionClosed:
                        break
                    if isinstance(msg, str):
                        try:
                            state = json.loads(msg)
                        except ValueError:
                            continue
                        kind = state.get("type") if isinstance(state, dict) else None
                        if kind == "hello":
                            on_event("hello", {"online": bool(state.get("cameraOnline")),
                                               "viewers": state.get("viewers")})
                        elif kind == "camera-offline":
                            on_event("offline", {})
                        continue
                    last = now()
                    if console:
                        try:
                            batch = json.loads(bytes(msg).decode("utf-8", "replace"))
                        except ValueError:
                            continue
                        if not isinstance(batch, dict):
                            continue
                        text = dedup.fresh(batch)
                        if text:
                            batches += 1
                            on_event("console", dict(batch, text=text))
                        continue
                    got += 1
                    on_event("frame", {"n": got, "stream": name, "data": bytes(msg)})
                    if frames and got >= frames:
                        return result()
            finally:
                ws.close()
        wait = budget()
        if wait is None:
            return result()
        pause = min(_BACKOFF_MAX_S, 2 ** tries, wait)
        tries += 1
        on_event("reconnect", {"after_s": pause})
        sleep(pause)

"""The device's one-shot HTTP readers take the body as it arrives -- they do not decode
chunked transfer encoding. A 1.1 request lets any server or proxy chunk the reply (Cloudflare
in front of the hosted OTA server does), which broke every check-in's JSON parse in the field
while the bench, talking straight to uvicorn, never saw it. Those requests must stay HTTP/1.0,
which no server may answer chunked."""
import os

DEVICE = os.path.join(os.path.dirname(__file__), "..", "..", "src", "openmv_ota", "build",
                      "device")


def _src(*parts):
    with open(os.path.join(DEVICE, *parts)) as f:
        return f.read()


def test_the_check_in_asks_for_an_unchunked_reply():
    src = _src("openmv_ota", "__init__.py")
    assert '"POST /api/v1/check HTTP/1.0' in src
    assert "/api/v1/check HTTP/1.1" not in src


def test_the_live_poll_asks_for_an_unchunked_reply():
    src = _src("openmv_cloud", "csi.py")
    poll = src[src.index("async def poll_watch"):]
    poll = poll[:poll.index("\nasync def ") if "\nasync def " in poll else len(poll)]
    assert "HTTP/1.0" in poll and "HTTP/1.1" not in poll

"""Host tests for ``openmv_cloud._lib`` -- the plumbing every wrapper shares.

The pure helpers that used to live in csi (URL parsing) and logs (session id,
record batching) and were reached into by sibling modules. The socket and
filesystem entry points (_open, _post_ndjson, _FileDisk, _open_disk,
_drain_disk) are device glue, exercised on hardware.
"""

from __future__ import annotations

import json

import pytest

from openmv_ota.build.device.openmv_cloud import _lib


# --- URL parsing ------------------------------------------------------------

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
    assert _lib._split_url(url) == want


@pytest.mark.parametrize("bad", ["ftp://x/y", "no-scheme", "https://"])
def test_split_url_rejects(bad):
    with pytest.raises(ValueError):
        _lib._split_url(bad)


# --- the backscroll contract: session id ------------------------------------

def test_session_id_is_hex_of_the_random_bytes():
    assert _lib._session_id(bytes(range(8))) == "0001020304050607"
    assert len(_lib._session_id()) == 16


# --- record batching --------------------------------------------------------

def _rec(sid, seq):
    return json.dumps({"sid": sid, "seq": seq, "text": "x\n"}).encode()


def test_batch_end_packs_records_within_max_bytes():
    recs = [b"12345", b"6789", b"abc"]                # +1 newline each when joined
    assert _lib._batch_end(recs, 0, 100) == 3         # all fit
    assert _lib._batch_end(recs, 0, 6) == 1           # only the first (5+1)
    assert _lib._batch_end(recs, 0, 1) == 1           # oversize still takes one
    assert _lib._batch_end(recs, 1, 6) == 2           # from the middle


def test_batch_end_never_crosses_a_sid_boundary():
    # a spool spanning a reboot: two sid runs, seq resets. A byte budget that
    # would swallow both runs must still stop at the boundary (one sid per batch).
    recs = [_rec("aa00", 0), _rec("aa00", 1), _rec("bb11", 0), _rec("bb11", 1)]
    assert _lib._batch_end(recs, 0, 10_000) == 2      # stops at the reboot boundary
    assert _lib._batch_end(recs, 2, 10_000) == 4      # the second run drains next


def test_rec_sid_of_a_non_record_line_is_none():
    assert _lib._rec_sid(b"12345") is None            # bare int
    assert _lib._rec_sid(b"not json") is None
    assert _lib._rec_sid(_rec("aa00", 0)) == "aa00"

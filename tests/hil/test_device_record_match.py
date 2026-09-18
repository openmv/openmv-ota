"""The harness has to find the board's row in the server's device table.

On 2026-09-15 the server started qualifying a device id with the board it came from
(`OPENMV_RT1060:9d7b4061d7292833`), because `machine.unique_id()` is only unique among
boards of the same TYPE. The UART reports the raw unit id, and the harness compared the
two with `==`. Nothing matched: every server-scored leg read `None/None` and waited out
its full 900 s timeout while the board had already installed, confirmed and promoted --
54 markers of a perfectly good cycle, scored as a failure.

The fleet gate had been broken since 2026-08-31 for an unrelated reason, so nothing
caught the drift for three days. This pins the matching itself.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil")))
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import pytest  # noqa: E402

import ota_cycle  # noqa: E402

UID = "9d7b4061d7292833"


@pytest.mark.parametrize("recorded", [
    "OPENMV_RT1060:9d7b4061d7292833",       # what the server keys by today
    "9d7b4061d7292833",                     # a device that reports no board -> raw id
])
def test_the_boards_own_row_is_found(recorded):
    assert ota_cycle._same_device(recorded, UID)


@pytest.mark.parametrize("recorded", [
    "OPENMV_RT1060:0000000000000000",       # same board type, different unit
    "9d7b4061d7292834",                     # one digit out
    "", None,                               # no record at all
])
def test_another_board_is_not_mistaken_for_it(recorded):
    assert not ota_cycle._same_device(recorded, UID)


def test_the_qualified_form_is_what_the_server_actually_builds():
    """Pinned against the server, not against a string I typed: if the composition
    changes again, this fails here rather than as a 900-second timeout on the bench."""
    from openmv_ota.server.app import _identity

    class _Req:
        board, device_id = "OPENMV_RT1060", UID

    assert _identity(_Req()) != UID                      # it really is qualified
    assert ota_cycle._same_device(_identity(_Req()), UID)

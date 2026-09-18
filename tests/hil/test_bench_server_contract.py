"""The bench server is built out of the real server's parts. This pins the seams.

Every leg's OTA server is `ci/hil/bench_server.py`, which imports private helpers out of
`openmv_ota.server.cli` and configures the app through `OPENMV_OTA_*` environment
variables. Both are couplings nothing else checks: rename a helper or a setting and the
bench server fails to start on a runner, an hour into a run, for a reason that looks
nothing like its cause.

This is the same class of drift that cost tonight three separate hours -- a publish flag
that was regrouped, a device id that became board-qualified -- caught here in a second
instead.
"""

from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BENCH = os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil", "bench_server.py"))
sys.path.insert(0, os.path.dirname(_BENCH))
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import pytest  # noqa: E402

from openmv_ota.server.settings import ServerSettings  # noqa: E402

_SOURCE = open(_BENCH).read()


def _imported_names():
    """``[(module, name)]`` for every `from openmv_ota... import ...` the file mentions,
    including the ones inside the server script it writes out as a string."""
    found = []
    for module, names in re.findall(r"from (openmv_ota[\w.]*) import ([\w, ]+)", _SOURCE):
        found += [(module, n.strip()) for n in names.split(",") if n.strip()]
    return found


def test_the_server_helpers_the_bench_calls_still_exist():
    names = _imported_names()
    assert names, "no openmv_ota imports found in bench_server.py -- did its shape change?"
    for module, name in names:
        mod = __import__(module, fromlist=[name])
        assert hasattr(mod, name), (
            "the bench server imports %s from %s, which no longer has it -- every leg would "
            "fail to start its server" % (name, module))


@pytest.mark.parametrize("var", sorted(set(re.findall(r"OPENMV_OTA_(\w+)=", _SOURCE))))
def test_the_settings_the_bench_configures_still_exist(var):
    assert var.lower() in ServerSettings.model_fields, (
        "the bench server sets OPENMV_OTA_%s, which the server no longer reads -- the legs "
        "would run against a differently-configured server and fail somewhere else" % var)

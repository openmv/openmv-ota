"""The device-path coverage AUDIT GATE.

Every line of device code (the code that ships to the camera and runs there, under
``# pragma: no cover`` in the host suite) must be ACCOUNTED for: either

  * witnessed -- it is a HIL log marker, or it is dominated by one (a marker proves every
    line BEFORE it on every path ran), so a real hardware run proves it executed; or
  * a declared residual -- a `# hil-residual: <reason>` (per line) / `# hil-residual-fn:
    <reason>` (on a def) that says WHY a marker can't witness it (a bare `return var`, a
    re-`raise`, a terminal reset/reboot, an error branch only reached by fault injection,
    the AE3 coprocessor path with no working HIL rig, the opt-in watchdog, ...).

A line that is neither is a GAP: a device path with no proof it is exercised. Once this
ships, an unexercised path is exactly where a field failure hides -- so this test fails CI
on the first unaccounted line, naming it, until it gets a marker or an honest residual.

This is the STATIC half (host, every push): it proves the accounting is complete assuming
every marker fires. The DYNAMIC half is the HIL run itself (ci/hil): ota_cycle's SCENARIOS
prove the markers actually fire on real N6 / RT1062 / AE3 hardware. Together: every device
line is accounted here, and every marker is shown to fire there.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "ci", "hil")))
os.environ.setdefault("WIFI_SSID", "")
os.environ.setdefault("WIFI_PASSWORD", "")

import pytest  # noqa: E402

import static_coverage  # noqa: E402


@pytest.mark.parametrize("path", static_coverage.DEFAULT_FILES,
                         ids=[os.path.relpath(p, static_coverage.DEVICE)
                              for p in static_coverage.DEFAULT_FILES])
def test_device_module_has_no_unaccounted_lines(path):
    r = static_coverage.analyze(path)
    gaps = r["gaps"]
    if gaps:
        owner, src = r["owner"], r["src"]
        detail = "\n".join("  %s:%d  %s  (%s)"
                           % (r["file"], ln, src[ln - 1].strip(), owner.get(ln, "?"))
                           for ln in gaps)
        pytest.fail(
            "%d unaccounted device line(s) in %s -- each must get a HIL log marker (or a "
            "line dominated by one), or a `# hil-residual: <reason>` explaining why a marker "
            "cannot witness it:\n%s" % (len(gaps), r["file"], detail))

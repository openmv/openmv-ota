"""Hypothesis profiles for the parser fuzz suite.

Two profiles, picked by ``HYPOTHESIS_PROFILE``:

- ``ci`` (the default): a modest, DERANDOMIZED example count so the suite stays fast and a
  red run is reproducible from the commit alone -- no example database, no seed drift.
- ``fuzz``: the hunting profile for long local runs, e.g.
  ``HYPOTHESIS_PROFILE=fuzz .venv/bin/pytest tests/fuzz -p no:cacheprovider --no-cov -n auto``.
  ``FUZZ_EXAMPLES`` overrides its per-property example count (default 20000).

The ``fuzz`` profile also carries a wall-clock ``deadline``: a parser that stalls on a crafted
input is a finding (a hang on the device is a watchdog bite at best), not something to wait out.
``ci`` does not: on a loaded CI runner a slow first example (app start-up, xdist contention) is
noise, and the hangs this suite has found are pinned by plain regression tests with their own
timing asserts.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

_SLOW_OK = [HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much]

settings.register_profile(
    "ci", max_examples=200, derandomize=True, database=None, deadline=None,
    suppress_health_check=_SLOW_OK, print_blob=True)
settings.register_profile(
    "fuzz", max_examples=int(os.environ.get("FUZZ_EXAMPLES", "20000")), deadline=5000,
    suppress_health_check=_SLOW_OK, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

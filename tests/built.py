"""What `build firmware` on this machine records, for tests that put a firmware binary in place
by hand: ``build romfs`` and ``flash`` refuse one with no record (openmv_ota.build.provenance)."""

from __future__ import annotations

import json
from pathlib import Path

from openmv_ota.build import provenance
from openmv_ota.project import lock as lock_mod
from openmv_ota.project.project import ProjectPaths

# The commits a bare test project's lock pins, when it has no real lock.
LOCK = {"lock_schema_version": 1, "firmware": {"commit": "f" * 40},
        "micropython": {"commit": "e" * 40}}


def record(root, *paths, inputs: str | None = "one build") -> None:
    """Record ``paths`` as built here for ``root`` from its lock, in one build (``inputs``),
    giving ``root`` a minimal lock when it has none."""
    lock_path = ProjectPaths(Path(root)).lock
    if not lock_path.exists():
        lock_path.write_text(json.dumps(LOCK))
    lock = lock_mod.read(lock_path)
    for path in paths:
        provenance.record(path, lock, root, inputs)

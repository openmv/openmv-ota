"""The committed lock (``openmv-ota.lock.json``): a portable, path-free snapshot.

It records identity (git remote + commit + submodule SHAs) and resolved values
(versions, per-board geometry) — never machine paths or live machine state, which
``status`` computes fresh. Drift comparison ignores the ``generated_*`` metadata.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .errors import ProjectError

LOCK_NAME = "openmv-ota.lock.json"
LOCK_SCHEMA_VERSION = 1

# Top-level keys compared for drift (generated_by/at are metadata, not state).
_DRIFT_KEYS = ("config_digest", "firmware", "micropython", "sdk", "toolchain",
               "submodules", "targets")


@dataclass
class Lock:
    generated_by: str
    generated_at: str
    config_digest: str
    firmware: dict
    micropython: dict
    sdk: dict
    toolchain: dict
    submodules: list
    targets: dict
    schema_version: int = LOCK_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "lock_schema_version": self.schema_version,
            "generated_by": self.generated_by,
            "generated_at": self.generated_at,
            "config_digest": self.config_digest,
            "firmware": self.firmware,
            "micropython": self.micropython,
            "sdk": self.sdk,
            "toolchain": self.toolchain,
            "submodules": self.submodules,
            "targets": self.targets,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Lock":
        return cls(
            schema_version=d.get("lock_schema_version", LOCK_SCHEMA_VERSION),
            generated_by=d.get("generated_by", ""),
            generated_at=d.get("generated_at", ""),
            config_digest=d.get("config_digest", ""),
            firmware=d.get("firmware", {}),
            micropython=d.get("micropython", {}),
            sdk=d.get("sdk", {}),
            toolchain=d.get("toolchain", {}),
            submodules=d.get("submodules", []),
            targets=d.get("targets", {}),
        )


def write(path: Path, lock: Lock) -> None:
    path.write_text(json.dumps(lock.to_dict(), indent=2) + "\n", encoding="utf-8")


def read(path: Path) -> Lock:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise ProjectError("no %s found (run `openmv-ota project new` first)" % LOCK_NAME) from None
    except json.JSONDecodeError as e:
        raise ProjectError("%s is corrupt: %s" % (LOCK_NAME, e)) from None
    if data.get("lock_schema_version") != LOCK_SCHEMA_VERSION:
        raise ProjectError(
            "%s has unsupported schema version %r (this tool writes %d)"
            % (LOCK_NAME, data.get("lock_schema_version"), LOCK_SCHEMA_VERSION)
        )
    return Lock.from_dict(data)


def drift(old: Lock, new: Lock) -> list[str]:
    """Return human-readable descriptions of substantive differences."""
    a, b = old.to_dict(), new.to_dict()
    changes: list[str] = []
    for key in _DRIFT_KEYS:
        _diff(a.get(key), b.get(key), key, changes)
    return changes


def _diff(old, new, path: str, changes: list[str]) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new)):
            _diff(old.get(k), new.get(k), "%s.%s" % (path, k), changes)
    elif old != new:
        changes.append("%s: %r -> %r" % (path, old, new))

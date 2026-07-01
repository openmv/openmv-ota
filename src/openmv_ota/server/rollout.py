"""The rollout decision -- pure, deterministic, no I/O.

The staged-% is a stable per-device hash: a device's staged/not-staged verdict never flips while
the percent is fixed, and raising the percent only *adds* devices (monotonic inclusion) -- the
"raise the % as confidence grows" model. Salting by ``rollout_id`` means a device unlucky in one
rollout isn't systematically the canary in the next.
"""

from __future__ import annotations

import hashlib


def staged_in(rollout_id: str, device_id: str, percent: float) -> bool:
    """Whether ``device_id`` is in the staged set of ``rollout_id`` at ``percent`` (0..100)."""
    if percent >= 100:
        return True
    if percent <= 0:
        return False
    h = hashlib.sha256(("%s:%s" % (rollout_id, device_id)).encode()).digest()
    return (int.from_bytes(h[:4], "big") % 10000) < percent * 100


def offers_update(*, current_payload_version: int, release_payload_version: int,
                  rollout_state: str, rollout_percent: float, rollout_id: str,
                  device_id: str) -> bool:
    """Whether the active rollout's release should be offered to this device (all gates pure)."""
    if rollout_state != "active":
        return False
    if release_payload_version <= current_payload_version:      # anti-rollback: never older/equal
        return False
    return staged_in(rollout_id, device_id, rollout_percent)


def should_autopause(failures: int, attempted: int, threshold: float) -> bool:
    """Whether a rollout's fallback rate has crossed its failure threshold (the safety valve)."""
    return attempted > 0 and (failures / attempted) > threshold

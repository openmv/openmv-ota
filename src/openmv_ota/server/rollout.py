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


def fallback_payload_version(slots: list[dict] | None) -> int | None:
    """The version this device would fall back to, or ``None`` if it did not say.

    The newest slot that is not the running one -- and only if it is bootable at all, which
    here means it carries a real image (a non-zero version). A blank second slot reports 0 and
    is answered as ``None``: "no fallback" and "did not tell us" are both unknown-to-us, and
    neither should be rendered to an operator as a version."""
    if not slots:
        return None
    other = next((s for s in slots if not s.get("running")), None)
    if other is None:
        return None
    return int(other.get("payload_version") or 0) or None


def running_body_sha256(slots: list[dict] | None) -> str | None:
    """The RUNNING slot's exact bytes (its trailer's ``body_sha256``), or ``None`` if the
    device did not say -- a pre-sha payload, or no slots at all. "" from the device (a
    trailer that would not parse) is answered as ``None`` too: both are unknown-to-us, and
    a delta base cannot be named by either."""
    if not slots:
        return None
    running = next((s for s in slots if s.get("running")), None)
    if running is None:
        return None
    return str(running.get("body_sha256") or "") or None


def settled(slots: list[dict] | None) -> bool:
    """Whether the device is in a position to take an update, from its reported slots.

    A device running an unconfirmed trial is NOT: the slot an install would write is the one
    holding its last proven release, so updating now would trade a known-good fallback for an
    unproven one -- and it would do it exactly when the device has said it is unsure of itself.
    Waiting costs one poll interval and ends the moment the device confirms.

    The DEVICE enforces this too, and that is the authoritative check (see
    ``openmv_ota._defer_install``): a device must be safe against a server that is older,
    self-hosted, or simply wrong. Doing it here as well means we do not mint a capability
    token and burn a rollout slot on an offer we know will be deferred.

    Unknown (a device that reports no slots -- a v1 payload, or single-image) is treated as
    settled: this gate exists to protect a fallback that such a device does not have."""
    if not slots or len(slots) < 2:
        return True
    running = next((s for s in slots if s.get("running")), None)
    if running is None:
        return True
    return not (running.get("pending") and not running.get("confirmed"))


def offers_update(*, current_payload_version: int, release_payload_version: int,
                  rollout_state: str, rollout_percent: float, rollout_id: str,
                  device_id: str, allow_downgrade: bool = False,
                  slots: list[dict] | None = None) -> bool:
    """Whether the active rollout's release should be offered to this device (all gates pure).

    ``allow_downgrade`` (the server's TEST-ONLY ``test_offer_downgrades``) relaxes the
    anti-rollback gate so a rollout can offer an older/equal release -- the input a correct
    server never generates, needed to exercise the DEVICE's own anti-rollback on hardware. It
    only affects what is OFFERED; the device still rejects the downgrade itself."""
    if rollout_state != "active":
        return False
    if not allow_downgrade and release_payload_version <= current_payload_version:  # anti-rollback
        return False
    if not settled(slots):                          # mid-trial: its fallback is worth more
        return False
    return staged_in(rollout_id, device_id, rollout_percent)


def should_autopause(failures: int, attempted: int, threshold: float) -> bool:
    """Whether a rollout's fallback rate has crossed its failure threshold (the safety valve)."""
    return attempted > 0 and (failures / attempted) > threshold

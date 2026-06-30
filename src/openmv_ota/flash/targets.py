"""Resolve a board to its flash backend and per-artifact target.

The ``flash`` block in ``boards.json`` is the source of truth. For ``dfu`` it carries ``usb``
(the ``vid:pid``), ``alt`` (logical artifact -> DFU alt-setting), and an optional ``file`` map
overriding an artifact's filename (the AE3's per-core ``firmware-M55_HP.bin``). For ``imx`` it
carries the ``sdphost``/``blhost`` sub-blocks (device ids, addresses, flashloader names) that
``flash.imx`` reads directly off ``raw``.
"""

from __future__ import annotations

from dataclasses import dataclass

from openmv_ota.romfs.boards import get_board

from .errors import FlashError

SUPPORTED_BACKENDS = ("dfu", "imx", "arduino")


@dataclass(frozen=True)
class FlashConfig:
    board: str
    backend: str
    raw: dict

    @property
    def usb(self) -> str:
        """The dfu ``vid:pid``."""
        return self.raw["usb"]

    def alt_of(self, artifact: str) -> int:
        """The DFU alt-setting for a logical artifact (``firmware``/``romfs``/``coprocessor``)."""
        alt = self.raw.get("alt", {})
        try:
            return int(alt[artifact])
        except KeyError:
            raise FlashError(
                "board %r has no %r flash target (configured: %s)"
                % (self.board, artifact, ", ".join(sorted(alt)) or "none")
            ) from None

    def filename(self, artifact: str, default: str) -> str:
        """The artifact's filename suffix (after ``<board>-``), board override or ``default``."""
        return self.raw.get("file", {}).get(artifact, default)

    def has(self, artifact: str) -> bool:
        """Whether this board flashes ``artifact``. The AE3's HE core ships *with* the
        firmware (the two core images can't be flashed separately), so it's keyed off the
        board config, not a user flag."""
        return artifact in self.raw.get("alt", {})


def flash_config(board: str) -> FlashConfig:
    """The resolved flash config for ``board``; raises ``FlashError`` if it can't be flashed."""
    try:
        cfg = get_board(board)
    except LookupError as e:
        raise FlashError(str(e)) from None
    raw = cfg.flash
    if not raw:
        raise FlashError("board %r has no flash configuration (not a flashable target yet)"
                         % board)
    backend = raw.get("backend")
    if backend == "unsupported":                     # a board we deliberately can't flash
        raise FlashError("board %r can't be flashed with this tool: %s"
                         % (board, raw.get("reason", "unsupported target")))
    if backend not in SUPPORTED_BACKENDS:
        raise FlashError("board %r uses the %r flash backend, not supported yet (have: %s)"
                         % (board, backend, ", ".join(SUPPORTED_BACKENDS)))
    return FlashConfig(board=board, backend=backend, raw=raw)

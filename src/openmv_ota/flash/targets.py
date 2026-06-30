"""Resolve a board to its flash backend and per-artifact target.

The ``flash`` block in ``boards.json`` is the source of truth: ``backend`` (which host tool),
``usb`` (the ``vid:pid`` for dfu), ``alt`` (a map of logical artifact -> DFU alt-setting), and
an optional ``file`` map overriding an artifact's default filename (the AE3's firmware is the
per-core ``firmware-M55_HP.bin``, not a plain ``firmware.bin``).
"""

from __future__ import annotations

from dataclasses import dataclass

from openmv_ota.romfs.boards import get_board

from .errors import FlashError

SUPPORTED_BACKENDS = ("dfu",)


@dataclass(frozen=True)
class FlashConfig:
    board: str
    backend: str
    usb: str
    alt: dict[str, int]
    files: dict[str, str]

    def alt_of(self, artifact: str) -> int:
        """The DFU alt-setting for a logical artifact (``firmware``/``romfs``/``coprocessor``)."""
        try:
            return self.alt[artifact]
        except KeyError:
            raise FlashError(
                "board %r has no %r flash target (configured: %s)"
                % (self.board, artifact, ", ".join(sorted(self.alt)) or "none")
            ) from None

    def filename(self, artifact: str, default: str) -> str:
        """The artifact's filename suffix (after ``<board>-``), board override or ``default``."""
        return self.files.get(artifact, default)


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
    if backend not in SUPPORTED_BACKENDS:
        raise FlashError("board %r uses the %r flash backend, not supported yet (have: %s)"
                         % (board, backend, ", ".join(SUPPORTED_BACKENDS)))
    return FlashConfig(board=board, backend=backend, usb=raw["usb"],
                       alt={k: int(v) for k, v in raw.get("alt", {}).items()},
                       files=dict(raw.get("file", {})))

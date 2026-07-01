"""The server subsystem's error type, mirroring ``project``/``flash``/``build``."""

from __future__ import annotations


class ServerError(Exception):
    """A server-side failure carrying a CLI exit code (2 = usage/precondition, 1 = operational)."""

    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code

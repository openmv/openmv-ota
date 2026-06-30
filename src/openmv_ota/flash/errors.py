"""Errors for the flash subsystem."""

from __future__ import annotations


class FlashError(Exception):
    """A board could not be flashed. Carries a CLI ``exit_code``."""

    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code

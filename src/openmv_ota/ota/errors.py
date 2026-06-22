"""Errors for the OTA subsystem."""

from __future__ import annotations


class OtaError(Exception):
    """An OTA trailer could not be built or parsed. Carries a CLI ``exit_code``."""

    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code

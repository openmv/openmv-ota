"""Errors for the build subsystem."""

from __future__ import annotations


class BuildError(Exception):
    """A romfs image could not be built. Carries a CLI ``exit_code``."""

    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code

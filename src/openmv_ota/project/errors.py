"""Errors for the project subsystem."""

from __future__ import annotations


class ProjectError(Exception):
    """A project could not be created, loaded, or resolved.

    Carries an ``exit_code`` so the CLI can map failures to ``2`` (usage /
    precondition, the default) or ``1`` (operational) without a lookup table.
    """

    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code

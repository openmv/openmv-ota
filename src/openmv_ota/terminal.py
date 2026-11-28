"""What this tool writes to a terminal.

Text that a server, a camera or a USB device chose reaches the screen in errors (a server's
``detail``, a camera's ``SE-ERROR`` line, a malformed identity line, a USB serial string), in
listings and in exports. A terminal acts on what it is sent: an escape sequence can rewrite what
is already on the screen, set the window title or reprogram keys, and a carriage return or a
bidirectional control hides what is written behind it. So while stdout or stderr is a terminal,
everything written there goes through :func:`shown`. Redirected output is left as it is: a file
or a pipe is data, and ``--json`` escapes its own.
"""

from __future__ import annotations

import re
import sys

# C0 but tab and newline, DEL and C1; the controls that reorder or hide text (ALM, the zero
# width marks, LRM/RLM, the embeddings and isolates, the line and paragraph separators, BOM)
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f؜​-‏  "
                      "‪-‮⁠-⁤⁦-⁩﻿]")


def shown(text: str) -> str:
    """``text`` with each control a terminal would act on printed as ``?`` (one for one, so a
    write's length is unchanged)."""
    return _CONTROL.sub("?", text)


class _Terminal:
    """A text stream that writes what :func:`shown` leaves of each string, and is otherwise
    the stream it wraps."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        return self._stream.write(shown(text))

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):     # no such method, or a closed stream
        return False


def guard() -> None:
    """Route ``sys.stdout`` and ``sys.stderr`` through :func:`shown` while each is a terminal."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is not None and not isinstance(stream, _Terminal) and _isatty(stream):
            setattr(sys, name, _Terminal(stream))

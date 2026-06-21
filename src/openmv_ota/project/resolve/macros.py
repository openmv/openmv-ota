"""Parse C ``#define`` macros out of a header file.

OpenMV writes its version constants as plain ``#define NAME value`` lines, with
the value sometimes wrapped in parentheses (e.g. ``#define
OMV_FIRMWARE_VERSION_MAJOR (5)``). This finds the requested names and strips a
single layer of surrounding parentheses and quotes.
"""

from __future__ import annotations

import re


def parse_defines(text: str, names: list[str]) -> dict[str, str]:
    """Return ``{name: value}`` for each requested name found in ``text``.

    Values keep their original token text minus one wrapping pair of ``()`` and
    minus surrounding double quotes. Names not present are simply absent from the
    result (callers decide whether that is an error).
    """
    wanted = set(names)
    found: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"\s*#\s*define\s+(\w+)\s+(.+?)\s*$", line)
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        if name not in wanted:
            continue
        # Drop a trailing line comment, then unwrap () and quotes.
        value = re.split(r"/[/*]", value, maxsplit=1)[0].strip()
        if value.startswith("(") and value.endswith(")"):
            value = value[1:-1].strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        found[name] = value
    return found

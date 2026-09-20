"""The docs are held to the code: every relative link and anchor resolves, every
``openmv-ota`` command line on a page parses against the real CLI, every ``/api/v1``
path a page names is a route the server serves, and every ``OPENMV_OTA_*`` variable a
page names is a setting. The introduction promises exactly this."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path

import pytest

from openmv_ota.cli import build_parser
from openmv_ota.server.app import create_app
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage

DOCS = Path(__file__).resolve().parents[1] / "docs"
PAGES = sorted(DOCS.rglob("*.md"))
TEXTS = {p: p.read_text(encoding="utf-8") for p in PAGES}


def _anchors(text: str) -> set[str]:
    """GitHub's heading -> anchor rule: lowercase, drop punctuation, spaces to dashes;
    underscores survive, backticks and links do not."""
    out = set()
    for m in re.finditer(r"(?m)^#{1,6}\s+(.*)$", text):
        h = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", m.group(1))
        h = h.replace("`", "").replace("*", "").strip()
        out.add(re.sub(r"[^\w\- ]", "", h.lower()).strip().replace(" ", "-"))
    return out


def test_every_link_and_anchor_resolves():
    anchors = {p: _anchors(t) for p, t in TEXTS.items()}
    bad = []
    for p, t in TEXTS.items():
        for m in re.finditer(r"\[[^\]]*\]\(([^)\s]+)\)", t):
            href = m.group(1)
            if href.startswith(("http://", "https://", "mailto:")):
                continue
            path, _, frag = href.partition("#")
            target = (p.parent / path).resolve() if path else p.resolve()
            if path and not target.exists():
                bad.append("%s -> %s (missing)" % (p.name, href))
            elif frag and target.suffix == ".md" and frag not in anchors.get(target, set()):
                bad.append("%s -> %s (no such anchor)" % (p.name, href))
    assert bad == []


def _subparsers(parser):
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return dict(a.choices)
    return {}


def _flags(parser):
    return {o for a in parser._actions for o in a.option_strings}


def _command_lines():
    for p, t in TEXTS.items():
        for block in re.findall(r"```(?:bash|sh|console|text)?\n(.*?)```", t, re.S):
            for ln in block.replace("\\\n", " ").splitlines():
                ln = ln.strip()
                if ln.startswith("$ "):
                    ln = ln[2:]
                if not ln.startswith("openmv-ota "):
                    continue
                ln = re.sub(r"\$\([^)]*\)", "X", ln)          # $(date ...) is not a flag
                ln = ln.split("#", 1)[0].split("|", 1)[0].strip()
                yield p, [x for x in re.split(r"\s+", ln)[1:] if x and x != "\\"
                          and not x.startswith("<")]


def test_every_command_line_parses_against_the_cli():
    root = build_parser()
    seen, bad = 0, []
    for p, toks in _command_lines():
        seen += 1
        parser, i = root, 0
        while i < len(toks) and toks[i] in _subparsers(parser):
            parser, i = _subparsers(parser)[toks[i]], i + 1
        if i == 0 and toks:
            bad.append("%s: unknown verb in `openmv-ota %s`" % (p.name, " ".join(toks)))
            continue
        for t in toks[i:]:
            if t.startswith("-") and t != "-" and not t[1:2].isdigit():   # `-o -` is stdout
                name = t.split("=", 1)[0]
                if name not in _flags(parser) | {"-h", "--help"}:
                    bad.append("%s: no flag %s on `openmv-ota %s`"
                               % (p.name, name, " ".join(toks[:i])))
    assert seen > 100                     # the tutorial is command-heavy; a silent zero is a bug
    assert bad == []


@pytest.fixture(scope="module")
def routes():
    d = tempfile.mkdtemp()
    store = SqliteMetadataStore(os.path.join(d, "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "x")
    app = create_app(ServerSettings(base_url="https://ota.test", swd_ids_verify_url="u",
                                    swd_ids_verify_token="t"),
                     metastore=store, storage=LocalArtifactStorage(os.path.join(d, "blobs")))
    return [r.split("/") for r in app.openapi()["paths"]]


def _served(path: str, routes) -> bool:
    segs = path.split("/")
    for r in routes:
        if len(r) == len(segs) and all(a == b or a.startswith("{") for a, b in zip(r, segs)):
            return True
    return False


def test_every_server_path_named_is_a_route(routes):
    bad = []
    for p, t in TEXTS.items():
        for m in re.finditer(r"(/api/v1/[A-Za-z0-9_{}/:.\-]+)", t):
            path = m.group(1).rstrip(".").split("?")[0]
            if "..." in path or "{" in path.split("/")[-1] and "}" not in path:
                continue
            # the datalake and the relay serve their own /api/v1 paths
            if any(s in path for s in ("/topics/", "/logs/", "/series/", "/products/")):
                continue
            if not _served(path, routes):
                bad.append("%s: %s" % (p.name, path))
    assert bad == []


def test_every_setting_named_exists():
    known = {"OPENMV_OTA_" + name.upper() for name in ServerSettings.model_fields}
    src = "".join(f.read_text(encoding="utf-8")
                  for f in (Path(__file__).resolve().parents[1] / "src").rglob("*.py"))
    bad = []
    for p, t in TEXTS.items():
        for var in sorted(set(re.findall(r"OPENMV_OTA_[A-Z0-9_]+", t))):
            if var not in known and var not in src:
                bad.append("%s: %s" % (p.name, var))
    assert bad == []

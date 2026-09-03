"""``build sbom`` -- a CycloneDX SBOM rendered from the committed lock.

The lock already records the full dependency pin-set of a build -- the firmware commit,
every submodule commit, the MicroPython version, and the resolved toolchain versions --
so the SBOM is a *renderer* over data the project always has, not new collection. It
needs only the committed project (config + lock + ``app/settings.json``): no firmware
checkout, no verify, so CI can export it from a bare clone.

Deterministic on purpose: the BOM's timestamp is the lock's ``generated_at`` and there
is no serial number, so the same lock always renders byte-identical output -- an SBOM
that changes only when the dependencies change is diffable evidence.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from openmv_ota import __version__
from openmv_ota.project import lock as lock_mod
from openmv_ota.project.config import load_config
from openmv_ota.project.errors import ProjectError
from openmv_ota.project.project import ProjectPaths

from .errors import BuildError

SBOM_NAME = "sbom.cdx.json"


def _github_purl(remote: str, commit: str) -> str | None:
    """A ``pkg:github`` purl when the remote is recognizably GitHub; None otherwise
    (never guess a package identity)."""
    marker = "github.com"
    if marker not in remote:
        return None
    tail = remote.split(marker, 1)[1].lstrip(":/")
    if tail.endswith(".git"):
        tail = tail[:-4]
    parts = [p for p in tail.split("/") if p]
    if len(parts) < 2:
        return None
    return "pkg:github/%s/%s@%s" % (parts[0], parts[1], commit)


def _props(**kv) -> list[dict]:
    """CycloneDX properties from keyword pairs, skipping empty values, sorted by name."""
    return [{"name": "openmv-ota:%s" % k, "value": str(v)}
            for k, v in sorted(kv.items()) if v not in (None, "")]


# SPDX detection by distinctive body text. Ordered: first match wins. Coarse on
# purpose -- the goal is a correct id for the licenses people actually ship
# firmware apps under, with honest fallbacks for everything else.
_LICENSE_MARKERS = (
    ("Apache License", "Apache-2.0"),
    ("GNU AFFERO GENERAL PUBLIC LICENSE", "AGPL-3.0-only"),
    ("GNU LESSER GENERAL PUBLIC LICENSE", "LGPL-3.0-only"),
    ("GNU GENERAL PUBLIC LICENSE", "GPL-3.0-only"),
    ("Mozilla Public License", "MPL-2.0"),
    ("Permission is hereby granted, free of charge", "MIT"),
    ("Redistribution and use in source and binary forms", "BSD-3-Clause"),
    ("This is free and unencumbered software released into the public domain",
     "Unlicense"),
)


def detect_license(text: str) -> dict:
    """One CycloneDX license entry for the project's LICENSE text: an SPDX id
    for the classics, {"name": "Proprietary"} for an all-rights-reserved file,
    {"name": "Custom"} for anything else."""
    for marker, spdx in _LICENSE_MARKERS:
        if marker.lower() in text.lower():
            return {"id": spdx}
    if "proprietary" in text.lower() or "all rights reserved" in text.lower():
        return {"name": "Proprietary"}
    return {"name": "Custom"}


def _app_identity(paths) -> dict:
    """The app's own exact identity: the PROJECT repo's HEAD (the customer's
    app is a git repo like every submodule), plus a dirty flag when the tree
    has uncommitted changes -- so "version 1.2.3" is pinned to real bytes.
    A project that is not a git repo simply makes no commit claim."""
    from openmv_ota.project import gitrepo

    try:
        if not gitrepo.is_git_repo(paths.root):
            return {}
        out = {"commit": gitrepo.head_commit(paths.root)}
        if gitrepo.is_dirty(paths.root):
            out["dirty"] = True
        return out
    except Exception:                              # noqa: BLE001 - identity is
        return {}                                  # best-effort, never a build error


def _app_licenses(paths) -> list | None:
    """The app's license, read from the project's LICENSE file (scaffolded as
    proprietary; the customer replaces it freely). No file = no claim."""
    lic = paths.root / "LICENSE"
    try:
        return [{"license": detect_license(lic.read_text(encoding="utf-8"))}]
    except OSError:
        return None


def _describe_version(describe: str, fallback: str) -> str:
    """The RELEASE version a submodule sits on, from its `git describe`:
    "v2.1.3-14-gabc123" -> "2.1.3" (release plus local commits -- the version
    vulnerability databases speak). No tag reachable = no release claim; the
    commit stays the version and the purl carries the exact identity anyway."""
    m = re.match(r"v?(\d[\w.]*?)(?:-\d+-g[0-9a-f]+)?$", describe or "")
    return m.group(1) if m else fallback


def generate_sbom(project: str | Path) -> dict:
    """The CycloneDX 1.5 document for a committed project, as a dict."""
    paths = ProjectPaths(Path(project))
    try:
        config = load_config(paths.config)
        lock = lock_mod.read(paths.lock)
    except ProjectError as e:
        raise BuildError(str(e), exit_code=1) from None
    try:
        settings = json.loads(paths.app_settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise BuildError("sbom needs a readable %s: %s" % (paths.app_settings, e),
                         exit_code=1) from None
    app_version = settings.get("app_version")
    if not app_version:
        raise BuildError("%s is missing app_version" % paths.app_settings, exit_code=1)

    fw, mp, tc = lock.firmware, lock.micropython, lock.toolchain
    fw_ref = "openmv@%s" % fw.get("commit", "")
    components = []

    fw_component = {
        "type": "firmware", "bom-ref": fw_ref, "name": "openmv",
        "version": fw.get("version", ""),
        "properties": _props(commit=fw.get("commit"), branch=fw.get("branch"),
                             describe=fw.get("describe"), dirty=fw.get("dirty")),
    }
    purl = _github_purl(fw.get("remote", ""), fw.get("commit", ""))
    if purl:
        fw_component["purl"] = purl
    if fw.get("remote"):
        fw_component["externalReferences"] = [{"type": "vcs", "url": fw["remote"]}]
    components.append(fw_component)

    components.append({
        "type": "library", "bom-ref": "micropython@%s" % mp.get("commit", ""),
        "name": "micropython", "version": mp.get("version", ""),
        "properties": _props(commit=mp.get("commit"),
                             mpy_abi_version=mp.get("mpy_abi_version")),
    })
    sub_refs = []
    for sub in lock.submodules:
        ref = "%s@%s" % (sub.get("path", ""), sub.get("commit", ""))
        sub_refs.append(ref)
        component = {
            "type": "library", "bom-ref": ref, "name": sub.get("path", ""),
            # the release version when a tag is reachable (what OSV/NVD match);
            # the exact commit stays in the bom-ref, purl, and properties
            "version": _describe_version(sub.get("describe", ""), sub.get("commit", "")),
            "properties": _props(commit=sub.get("commit"), describe=sub.get("describe")),
        }
        purl = _github_purl(sub.get("remote", ""), sub.get("commit", ""))
        if purl:
            component["purl"] = purl
        if sub.get("remote"):
            component["externalReferences"] = [{"type": "vcs", "url": sub["remote"]}]
        components.append(component)
    tool_refs = []
    if lock.sdk.get("version"):
        tool_refs.append("openmv-sdk@%s" % lock.sdk["version"])
        components.append({"type": "application", "bom-ref": tool_refs[-1],
                           "name": "openmv-sdk", "version": lock.sdk["version"]})
    mpy_cross = tc.get("mpy_cross", {}).get("version")
    if mpy_cross:
        tool_refs.append("mpy-cross@%s" % mpy_cross)
        components.append({"type": "application", "bom-ref": tool_refs[-1],
                           "name": "mpy-cross", "version": mpy_cross,
                           "purl": "pkg:pypi/mpy-cross@%s" % mpy_cross})
    for name in ("vela", "stedgeai"):
        version = tc.get(name, {}).get("version")
        if version:
            tool_refs.append("%s@%s" % (name, version))
            components.append({"type": "application", "bom-ref": tool_refs[-1],
                               "name": name, "version": version})

    root_ref = "%s@%s" % (config.name, app_version)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            # the LOCK's timestamp, so the same lock renders byte-identical output
            "timestamp": lock.generated_at,
            "tools": {"components": [{"type": "application", "name": "openmv-ota",
                                      "version": __version__}]},
            "component": {
                "type": "firmware", "bom-ref": root_ref, "name": config.name,
                "version": app_version,
                **({"licenses": lic} if (lic := _app_licenses(paths)) else {}),
                "properties": _props(boards=",".join(config.boards),
                                     vendor=config.vendor or "", ota=lock.ota,
                                     **_app_identity(paths)),
            },
        },
        "components": components,
        "dependencies": [
            {"ref": root_ref, "dependsOn": [fw_ref, *tool_refs]},
            {"ref": fw_ref,
             "dependsOn": ["micropython@%s" % mp.get("commit", ""), *sub_refs]},
        ],
    }


def render_sbom(project: str | Path) -> str:
    """The document as stable, diffable JSON text (sorted keys, trailing newline)."""
    return json.dumps(generate_sbom(project), indent=2, sort_keys=True) + "\n"

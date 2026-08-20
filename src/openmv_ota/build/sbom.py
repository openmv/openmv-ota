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
            "version": sub.get("commit", ""),
            "properties": _props(describe=sub.get("describe")),
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
                "properties": _props(boards=",".join(config.boards),
                                     vendor=config.vendor or "", ota=lock.ota),
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

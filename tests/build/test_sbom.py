"""Tests for `build sbom` -- the CycloneDX renderer over the committed lock."""

import json

import pytest

from openmv_ota.build import sbom as sbom_mod
from openmv_ota.build.errors import BuildError
from openmv_ota.project import lock as lock_mod


def _project(tmp_path, *, remote="https://github.com/openmv/openmv.git",
             settings='{"app_version": "1.2.3", "vendor": "Acme"}\n', vela="3.12.0"):
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "openmv-ota.toml").write_text(
        '[product]\nname = "widget"\n\n[targets]\nboards = ["OPENMV_N6"]\n')
    (root / "app" / "settings.json").write_text(settings)
    lock_mod.write(root / lock_mod.LOCK_NAME, lock_mod.Lock(
        generated_by="openmv-ota test", generated_at="2026-08-19T00:00:00Z",
        config_digest="sha256:d", ota=True,
        firmware={"version": "5.0.0", "remote": remote,
                  "commit": "9f2c1ab3d4e5f60718293a4b5c6d7e8f90a1b2c3",
                  "branch": "master", "describe": "v5.0.0", "dirty": False},
        micropython={"commit": "aa" * 20, "version": "1.28.0", "mpy_abi_version": 6},
        sdk={"version": "1.6.0"},
        toolchain={"mpy_cross": {"version": "1.28.0"},
                   "vela": {"version": vela, "found": bool(vela)},
                   "stedgeai": {"version": None, "found": False}},
        submodules=[{"path": "lib/micropython", "commit": "aa" * 20, "describe": "v1.28.0",
                     "remote": "https://github.com/openmv/micropython.git"},
                    {"path": "lib/lwip", "commit": "bb" * 20, "describe": "", "remote": ""}],
        targets={"boards": ["OPENMV_N6"], "resolved": []},
    ))
    return root


def test_sbom_shape_and_determinism(tmp_path):
    root = _project(tmp_path)
    doc = sbom_mod.generate_sbom(root)
    assert (doc["bomFormat"], doc["specVersion"]) == ("CycloneDX", "1.5")
    assert doc["metadata"]["timestamp"] == "2026-08-19T00:00:00Z"   # the LOCK's, not now()
    assert "serialNumber" not in doc                                # deterministic on purpose
    assert sbom_mod.render_sbom(root) == sbom_mod.render_sbom(root)

    root_c = doc["metadata"]["component"]
    assert (root_c["name"], root_c["version"], root_c["type"]) == ("widget", "1.2.3", "firmware")

    by_name = {c["name"]: c for c in doc["components"]}
    fw = by_name["openmv"]
    assert fw["purl"] == "pkg:github/openmv/openmv@9f2c1ab3d4e5f60718293a4b5c6d7e8f90a1b2c3"
    assert fw["externalReferences"] == [{"type": "vcs",
                                         "url": "https://github.com/openmv/openmv.git"}]
    assert by_name["micropython"]["version"] == "1.28.0"
    assert by_name["lib/micropython"]["purl"] == "pkg:github/openmv/micropython@" + "aa" * 20
    assert by_name["lib/lwip"]["version"] == "bb" * 20
    assert "purl" not in by_name["lib/lwip"]                     # no remote -> never guessed
    assert by_name["mpy-cross"]["purl"] == "pkg:pypi/mpy-cross@1.28.0"
    assert by_name["vela"]["version"] == "3.12.0"
    assert "stedgeai" not in by_name                                # no version -> no component

    deps = {d["ref"]: d["dependsOn"] for d in doc["dependencies"]}
    assert fw["bom-ref"] in deps["widget@1.2.3"]
    assert "lib/lwip@" + "bb" * 20 in deps[fw["bom-ref"]]


def test_sbom_non_github_remote_gets_no_purl(tmp_path):
    root = _project(tmp_path, remote="https://git.example.com/openmv.git")
    fw = {c["name"]: c for c in sbom_mod.generate_sbom(root)["components"]}["openmv"]
    assert "purl" not in fw and fw["externalReferences"][0]["url"].startswith("https://git.example")


def test_github_purl_edge_cases():
    f = sbom_mod._github_purl
    assert f("git@github.com:openmv/openmv.git", "c1") == "pkg:github/openmv/openmv@c1"
    assert f("https://github.com/openmv", "c1") is None              # no repo part
    assert f("https://example.com/x.git", "c1") is None


def test_sbom_errors(tmp_path):
    with pytest.raises(BuildError, match="no openmv-ota.toml"):
        sbom_mod.generate_sbom(tmp_path)                             # not a project
    root = _project(tmp_path, settings="{}\n")
    with pytest.raises(BuildError, match="missing app_version"):
        sbom_mod.generate_sbom(root)
    (root / "app" / "settings.json").unlink()
    with pytest.raises(BuildError, match="readable"):
        sbom_mod.generate_sbom(root)


def test_sbom_cli_writes_and_stdout(tmp_path, capsys):
    from openmv_ota.cli import main
    root = _project(tmp_path)
    assert main(["build", "sbom", str(root)]) == 0
    out = capsys.readouterr().out
    assert "sbom.cdx.json" in out and "components)" in out
    doc = json.loads((root / "build" / "sbom.cdx.json").read_text())
    assert doc["bomFormat"] == "CycloneDX"

    assert main(["build", "sbom", str(root), "-o", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["specVersion"] == "1.5"

    assert main(["build", "sbom", str(tmp_path / "nope")]) == 1
    assert "error:" in capsys.readouterr().err

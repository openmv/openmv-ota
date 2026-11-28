"""A firmware binary is signed into a release, or flashed, only as this machine's `build firmware`
wrote it. A project handed over as a directory or an archive (or with build/ committed) could
carry a planted `build/<board>-firmware.bin`, and `build romfs` signed it in with the
engineer's keys; with `-o` elsewhere it fell back to the project's build/ without a word."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from openmv_ota.build import firmware as fw
from openmv_ota.build import provenance
from openmv_ota.build import romfs as build_mod
from openmv_ota.build.errors import BuildError
from openmv_ota.ota import bundle, components, parse_trailer
from openmv_ota.project import lock as lock_mod
from openmv_ota.project.project import ProjectPaths
from tests.build.conftest import FAKE_FIRMWARE
from tests.build.test_firmware import _fake_make

PLANTED = b"NOT-BUILT-HERE: planted firmware " * 4096
_FILES = {"main.py": "print(1)\n", "settings.json": '{"app_version": "1.2.3", "vendor": "Acme"}\n'}


def _romfs(root, repo, app, **kw):
    return build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False,
                                 convert_models=False, **kw)


def _packed_firmware(result) -> bytes:
    body, trailer = bundle.read_bundle(result.output)
    comp = parse_trailer(trailer).components[0]
    return components.decompress(body[comp.offset:comp.offset + comp.size], comp.installed_size)


def test_a_firmware_planted_in_a_handed_over_project_is_not_signed(make_project, tmp_path,
                                                                   monkeypatch):
    """The project came with its build/: this machine has no record of building any of it."""
    root, repo, app = make_project(ota=True, app_files=_FILES)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "another-machine"))
    (root / "build" / "OPENMV_N6-firmware.bin").write_bytes(PLANTED)
    with pytest.raises(BuildError, match=r"OPENMV_N6-firmware\.bin \(sha256 %s\) was not built by "
                       r"`openmv-ota build firmware` on this machine"
                       % hashlib.sha256(PLANTED).hexdigest()) as e:
        _romfs(root, repo, app)
    assert e.value.exit_code == 1
    assert not (root / "build" / "OPENMV_N6-romfs.zip").exists()


def test_a_firmware_replaced_after_build_firmware_wrote_it_is_not_signed(make_project):
    root, repo, app = make_project(ota=True, app_files=_FILES)
    (root / "build" / "OPENMV_N6-firmware.bin").write_bytes(PLANTED)
    with pytest.raises(BuildError, match=r"changed since `openmv-ota build firmware` wrote it "
                       r"\(sha256 %s\)" % hashlib.sha256(FAKE_FIRMWARE).hexdigest()):
        _romfs(root, repo, app)


@pytest.mark.parametrize("board, name, kw", [
    ("OPENMV_AE3", "OPENMV_AE3-firmware-M55_HP.bin", {}),
    ("OPENMV_AE3", "OPENMV_AE3-firmware-M55_HE.bin", {}),       # the second core's
    ("OPENMV4P", "OPENMV4P-firmware-text2.bin", {"ca": "tiny"}),  # the second text region
])
def test_every_binary_the_stub_installs_is_checked(make_project, board, name, kw):
    root, repo, app = make_project(boards=(board,), ota=True, app_files=_FILES, **kw)
    (root / "build" / name).write_bytes(PLANTED)
    with pytest.raises(BuildError, match=name):
        _romfs(root, repo, app, boards=[board])


def test_a_firmware_built_from_other_commits_is_not_signed(make_project):
    """Built here, but before `project sync` moved the lock to another firmware commit."""
    root, repo, app = make_project(ota=True, app_files=_FILES)
    old = lock_mod.read(ProjectPaths(root).lock)
    old.firmware = dict(old.firmware, commit="0" * 40)
    provenance.record(root / "build" / "OPENMV_N6-firmware.bin", old, root, "one build")
    with pytest.raises(BuildError, match=r"was built from firmware 0{40}, not the [0-9a-f]{40} the "
                       r"lock pins -- run `openmv-ota build firmware`"):
        _romfs(root, repo, app)


def test_the_packed_firmware_is_named_with_its_sha256(make_project, capsys):
    root, repo, app = make_project(ota=True, app_files=_FILES)
    r = _romfs(root, repo, app)[0]
    assert _packed_firmware(r) == FAKE_FIRMWARE
    assert "note: OPENMV_N6 packs %s (sha256 %s)" % (
        root / "build" / "OPENMV_N6-firmware.bin", hashlib.sha256(FAKE_FIRMWARE).hexdigest()) \
        in capsys.readouterr().err


def test_an_output_dir_without_firmware_is_an_error_not_the_projects_build(make_project, tmp_path):
    root, repo, app = make_project(ota=True, app_files=_FILES)
    other = tmp_path / "elsewhere"
    with pytest.raises(BuildError, match=r"%s not found: .* run `openmv-ota build firmware -o %s` "
                       r"first" % (other / "OPENMV_N6-firmware.bin", other)):
        _romfs(root, repo, app, output=other)


def test_what_build_firmware_writes_is_what_build_romfs_signs(make_project, monkeypatch, tmp_path):
    """The whole flow on one machine, with `-o` elsewhere on both: the record is made where the
    binary is written, and the romfs build there finds it."""
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    monkeypatch.setattr(fw, "_relink_text2", lambda *a: None)
    root, repo, app = make_project(ota=True, app_files=_FILES)
    (repo / ".git" / "info").mkdir(parents=True, exist_ok=True)   # as the real tree ignores it
    (repo / ".git" / "info" / "exclude").write_text("build/\n")
    out = tmp_path / "out"
    written = fw.build_firmware(root, firmware=repo, output=out)[0].outputs
    assert {p.name for p in written} >= {"OPENMV_N6-firmware.bin", "OPENMV_N6-stub.bin"}
    inputs = (repo / "build" / "OPENMV_N6" / fw._INPUTS_STAMP).read_text().strip()
    for p in written:                                       # every binary it wrote is recorded
        entry = json.loads(provenance._record_path(str(p.resolve())).read_text())
        assert entry["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
        assert entry["size"] == p.stat().st_size and entry["path"] == str(p.resolve())
        assert entry["firmware_commit"] == lock_mod.read(ProjectPaths(root).lock).firmware["commit"]
        assert entry["project"] == str(root.resolve()) and entry["inputs"] == inputs
    assert _packed_firmware(_romfs(root, repo, app, output=out)[0]) == b"FW"


def test_the_record_is_the_machines_and_only_its_owner_reads_it(make_project, monkeypatch):
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    root, repo, _app = make_project()
    fw.build_firmware(root, firmware=repo)
    path = root / "build" / "OPENMV_N6-firmware.bin"
    rec = provenance._record_path(str(path.resolve()))
    assert root not in rec.parents and rec.stat().st_mode & 0o777 == 0o600


def test_a_corrupt_record_is_no_record(make_project, tmp_path):
    root, repo, app = make_project(ota=True, app_files=_FILES)
    path = root / "build" / "OPENMV_N6-firmware.bin"
    provenance._record_path(str(path.resolve())).write_text("{not json")
    with pytest.raises(BuildError, match="was not built by"):
        _romfs(root, repo, app)
    provenance._record_path(str(path.resolve())).write_text(json.dumps(
        {"path": str(tmp_path / "elsewhere.bin"), "sha256": hashlib.sha256(FAKE_FIRMWARE).hexdigest()}))
    with pytest.raises(BuildError, match="was not built by"):    # a record for another path
        _romfs(root, repo, app)


# --- where the record lives -----------------------------------------------------------------

def test_the_state_dir_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    assert provenance.state_dir("posix") == tmp_path / "st" / "openmv-ota"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert provenance.state_dir("posix") == Path.home() / ".local" / "state" / "openmv-ota"


def test_a_relative_xdg_state_home_is_ignored(monkeypatch):
    """Read against the working directory, it would be the project's to write."""
    monkeypatch.setenv("XDG_STATE_HOME", "state")
    assert provenance.state_dir("posix") == Path.home() / ".local" / "state" / "openmv-ota"


def test_the_state_dir_on_windows(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert provenance.state_dir("nt") == tmp_path / "openmv-ota" / "state"
    monkeypatch.delenv("LOCALAPPDATA")
    assert provenance.state_dir("nt") == Path.home() / "AppData" / "Local" / "openmv-ota" / "state"


def test_a_shared_output_dir_does_not_pack_another_projects_firmware(make_project, tmp_path):
    """Two projects pinned to one commit, building into one `-o` directory: B's romfs build
    packed A's firmware, with A's payload keys and frozen modules."""
    root, repo, app = make_project(ota=True, app_files=_FILES)
    path = root / "build" / "OPENMV_N6-firmware.bin"
    other = tmp_path / "project-a"
    other.mkdir()
    provenance.record(path, lock_mod.read(ProjectPaths(root).lock), other, "a's build")
    with pytest.raises(BuildError, match=r"was built for the project at %s -- run `openmv-ota "
                       r"build firmware` for this one" % other.resolve()):
        _romfs(root, repo, app)


def test_firmware_and_a_second_region_from_two_builds_are_not_packed(make_project):
    root, repo, app = make_project(boards=("OPENMV4P",), ota=True, app_files=_FILES, ca="tiny")
    lock = lock_mod.read(ProjectPaths(root).lock)
    provenance.record(root / "build" / "OPENMV4P-firmware-text2.bin", lock, root, "an earlier build")
    with pytest.raises(BuildError, match="were written by different `openmv-ota build firmware` runs"):
        _romfs(root, repo, app, boards=["OPENMV4P"])


def test_a_board_build_that_fails_at_its_stub_records_nothing(make_project, monkeypatch, tmp_path):
    """The firmware was recorded before the stub was built: a stub that failed left new firmware
    and the old stub, each passing its check."""
    from openmv_ota.build import stub as stub_mod
    monkeypatch.setattr(fw, "_run_make", _fake_make(["bin/firmware.bin"]))
    monkeypatch.setattr(fw, "_relink_text2", lambda *a: None)
    root, repo, app = make_project(ota=True, app_files=_FILES)
    (repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("build/\n")

    def broken(args):
        raise BuildError("stub build failed (make): exit 2")
    monkeypatch.setattr(stub_mod, "_run_make", broken)
    out = tmp_path / "out"
    with pytest.raises(BuildError, match="stub build failed"):
        fw.build_firmware(root, firmware=repo, output=out)
    assert (out / "OPENMV_N6-firmware.bin").read_bytes() == b"FW"
    with pytest.raises(BuildError, match="was not built by `openmv-ota build firmware`"):
        _romfs(root, repo, app, output=out)

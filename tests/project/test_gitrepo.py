"""Tests for the git porcelain wrapper and the mutating seams."""

from __future__ import annotations

import subprocess

import pytest

from openmv_ota.project import gitrepo
from openmv_ota.project.errors import ProjectError


def test_run_git_and_state(make_firmware):
    repo = make_firmware()
    assert gitrepo.is_git_repo(repo)
    assert len(gitrepo.head_commit(repo)) == 40
    assert gitrepo.current_branch(repo) == "main"
    assert gitrepo.describe(repo)  # any-commit fallback always returns something
    assert gitrepo.is_dirty(repo) is False
    assert gitrepo.remote_url(repo) == "git@github.com:openmv/openmv.git"


def test_dirty_and_no_remote(make_firmware):
    repo = make_firmware(with_remote=False)
    (repo / "SDK_VERSION").write_text("9.9.9")  # uncommitted change
    assert gitrepo.is_dirty(repo) is True
    assert gitrepo.remote_url(repo) is None


def test_detached_head(make_firmware):
    repo = make_firmware()
    sha = gitrepo.head_commit(repo)
    gitrepo.run_git(repo, "checkout", "-q", sha)
    assert gitrepo.current_branch(repo) is None


def test_is_git_repo_false(tmp_path):
    assert gitrepo.is_git_repo(tmp_path) is False


def test_is_git_repo_no_git_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(gitrepo, "GIT", "definitely-not-git-binary-xyz")
    assert gitrepo.is_git_repo(tmp_path) is False


def test_run_git_check_false_returns_none(make_firmware):
    assert gitrepo.run_git(make_firmware(), "rev-parse", "nope", check=False) is None


def test_run_git_check_true_raises(make_firmware):
    with pytest.raises(ProjectError, match="git rev-parse nope failed"):
        gitrepo.run_git(make_firmware(), "rev-parse", "nope")


def test_run_git_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(gitrepo, "GIT", "definitely-not-git-binary-xyz")
    with pytest.raises(ProjectError, match="git not found"):
        gitrepo.run_git(tmp_path, "status")


def test_submodule_status_parsing(monkeypatch, tmp_path):
    out = (
        " 1111111111111111111111111111111111111111 lib/micropython (v1.28.0)\n"
        "-2222222222222222222222222222222222222222 lib/uninit\n"
        "+3333333333333333333333333333333333333333 modules/ulab (6.12)\n"
        "\n"
        "garbage\n"
    )
    monkeypatch.setattr(gitrepo, "run_git", lambda *a, **k: out)
    entries = gitrepo.submodule_status(tmp_path)
    assert {e["path"] for e in entries} == {"lib/micropython", "lib/uninit", "modules/ulab"}
    by_path = {e["path"]: e for e in entries}
    assert by_path["lib/micropython"]["describe"] == "v1.28.0"
    assert by_path["lib/uninit"]["initialized"] is False
    assert by_path["modules/ulab"]["initialized"] is True


def test_submodule_remotes_parsing(tmp_path, git_cmd):
    # a REAL .gitmodules read through git config --file (no submodule checkout needed)
    repo = tmp_path / "r"
    repo.mkdir()
    git_cmd(repo, "init", "-q")
    (repo / ".gitmodules").write_text(
        '[submodule "lib/lwip"]\n\tpath = lib/lwip\n\turl = https://github.com/lwip-tcpip/lwip.git\n'
        '[submodule "weird"]\n\turl = https://example.com/only-url.git\n')
    assert gitrepo.submodule_remotes(repo) == {
        "lib/lwip": "https://github.com/lwip-tcpip/lwip.git"}   # path+url joined; url-only skipped
    (repo / ".gitmodules").unlink()
    assert gitrepo.submodule_remotes(repo) == {}                 # no .gitmodules -> empty


def test_submodule_remotes_skips_bare_keys(monkeypatch, tmp_path):
    # an EMPTY config value prints as a key-only line -- skipped, not crashed on
    monkeypatch.setattr(gitrepo, "run_git", lambda *a, **k:
                        "submodule.empty.path\n"
                        "submodule.lib/x.path lib/x\nsubmodule.lib/x.url https://e.com/x.git")
    assert gitrepo.submodule_remotes(tmp_path) == {"lib/x": "https://e.com/x.git"}


def test_resolve_submodules_carries_remotes(monkeypatch, tmp_path):
    from openmv_ota.project.resolve.submodules import resolve_submodules
    monkeypatch.setattr(gitrepo, "submodule_status",
                        lambda repo: [{"path": "lib/lwip", "commit": "c1", "describe": None,
                                       "initialized": True},
                                      {"path": "lib/other", "commit": "c2", "describe": None,
                                       "initialized": True}])
    monkeypatch.setattr(gitrepo, "submodule_remotes",
                        lambda repo: {"lib/lwip": "https://github.com/lwip-tcpip/lwip.git"})
    subs = {e["path"]: e for e in resolve_submodules(tmp_path)}
    assert subs["lib/lwip"]["remote"] == "https://github.com/lwip-tcpip/lwip.git"
    assert subs["lib/other"]["remote"] == ""                     # unmapped -> explicit empty


def test_submodule_status_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(gitrepo, "run_git", lambda *a, **k: None)
    assert gitrepo.submodule_status(tmp_path) == []


# --- mutating seams (mock subprocess) ---------------------------------------

class _FakeRun:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail  # exception to raise, or None

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.fail is not None:
            raise self.fail
        return subprocess.CompletedProcess(cmd, 0)


def test_clone_with_commit(monkeypatch, tmp_path):
    fake = _FakeRun()
    monkeypatch.setattr(gitrepo.subprocess, "run", fake)
    gitrepo.clone("git@x:openmv.git", tmp_path / "dst", commit="abc")
    assert any("clone" in c[0] for c in fake.calls)
    assert any("checkout" in c[0] for c in fake.calls)


def test_clone_without_commit(monkeypatch, tmp_path):
    fake = _FakeRun()
    monkeypatch.setattr(gitrepo.subprocess, "run", fake)
    gitrepo.clone("git@x:openmv.git", tmp_path / "dst")
    assert not any("checkout" in c[0] for c in fake.calls)


def test_clone_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(gitrepo.subprocess, "run",
                        _FakeRun(fail=subprocess.CalledProcessError(1, "git")))
    with pytest.raises(ProjectError) as ei:
        gitrepo.clone("r", tmp_path / "d")
    assert ei.value.exit_code == 1


def test_submodule_update(monkeypatch, tmp_path):
    fake = _FakeRun()
    monkeypatch.setattr(gitrepo.subprocess, "run", fake)
    gitrepo.submodule_update(tmp_path)
    assert any("submodule" in c[0] for c in fake.calls)


def test_submodule_update_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(gitrepo.subprocess, "run", _FakeRun(fail=FileNotFoundError()))
    with pytest.raises(ProjectError):
        gitrepo.submodule_update(tmp_path)


def test_pip_install(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(gitrepo.subprocess, "run", fake)
    gitrepo.pip_install("mpy-cross==1.28.0")
    assert fake.calls[0][0][1:] == ["-m", "pip", "install", "mpy-cross==1.28.0"]


def test_pip_install_failure(monkeypatch):
    monkeypatch.setattr(gitrepo.subprocess, "run",
                        _FakeRun(fail=subprocess.CalledProcessError(1, "pip")))
    with pytest.raises(ProjectError) as ei:
        gitrepo.pip_install("mpy-cross==1.28.0")
    assert ei.value.exit_code == 1

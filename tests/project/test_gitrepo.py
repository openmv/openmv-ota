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


def test_submodule_status_keeps_the_first_sha_whole(monkeypatch, tmp_path):
    """run_git strips its output, and with it the FIRST line's leading-space flag. Reading
    line[0] as the flag regardless ate the first character of that submodule's sha -- in
    the lock and the SBOM (lib/apriltag 636b9ba... recorded as 36b9ba..., which OSV then
    refused, failing the whole CVE scan)."""
    out = (" 636b9ba14ba8485c9fc09590cd4ef53f11427966 lib/apriltag (heads/master)\n"
           " 1111111111111111111111111111111111111111 lib/micropython (v1.28.0)\n").strip()
    monkeypatch.setattr(gitrepo, "run_git", lambda *a, **k: out)
    by_path = {e["path"]: e for e in gitrepo.submodule_status(tmp_path)}
    assert by_path["lib/apriltag"]["commit"] == "636b9ba14ba8485c9fc09590cd4ef53f11427966"
    assert by_path["lib/apriltag"]["initialized"] is True
    assert by_path["lib/micropython"]["commit"] == "1" * 40


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
    gitrepo.clone("git@x:openmv.git", tmp_path / "dst", commit="ab" * 20)
    assert any("clone" in c[0] for c in fake.calls)
    assert any("checkout" in c[0] for c in fake.calls)


def test_clone_takes_nothing_from_a_lock_file_as_an_option(monkeypatch, tmp_path):
    """The remote and the commit come from openmv-ota.lock.json, which someone else may have
    written: `--upload-pack=...` as a remote would be a command."""
    fake = _FakeRun()
    monkeypatch.setattr(gitrepo.subprocess, "run", fake)
    gitrepo.clone("--upload-pack=touch /tmp/x;", tmp_path / "dst")
    argv = fake.calls[0][0]
    assert argv[argv.index("clone") + 1] == "--"            # the remote is past the options
    for commit in ("abc", "--orphan", "ab" * 20 + ":x", "HEAD"):
        with pytest.raises(ProjectError, match="not a commit hash"):
            gitrepo.clone("r", tmp_path / "d2", commit=commit)
    assert len(fake.calls) == 1                             # refused before git ran


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


def test_git_runs_nothing_the_tree_s_own_config_names(make_firmware, tmp_path):
    """A firmware tree is one the project was pointed at. Its config can name a program for
    git to run on `status` (a filesystem monitor); reading the tree must not run it."""
    repo = make_firmware()
    ran = tmp_path / "ran"
    hook = tmp_path / "monitor.sh"
    hook.write_text("#!/bin/sh\ntouch %s\n" % ran)
    hook.chmod(0o755)
    gitrepo.run_git(repo, "config", "core.fsmonitor", str(hook))
    gitrepo.run_git(repo, "status", "--porcelain")
    assert not ran.exists()


def test_git_runs_no_filter_the_tree_s_own_config_defines(make_firmware, tmp_path, monkeypatch):
    """`git status` runs a content filter's `clean` program on a changed file."""
    repo = make_firmware()
    ran = tmp_path / "ran"
    prog = tmp_path / "clean.sh"
    prog.write_text("#!/bin/sh\ntouch %s\ncat\n" % ran)
    prog.chmod(0o755)
    tracked = [p for p in repo.iterdir() if p.is_file() and p.name != ".gitattributes"][0]
    (repo / ".gitattributes").write_text("* filter=x\n")
    gitrepo.run_git(repo, "config", "filter.x.clean", str(prog))
    tracked.write_bytes(tracked.read_bytes()[::-1] or b"x")
    gitrepo.run_git(repo, "status", "--porcelain")
    assert gitrepo.is_dirty(repo) and not ran.exists()
    monkeypatch.setattr(gitrepo, "GIT", str(tmp_path / "no-such-git"))
    assert gitrepo._no_filters(repo) == {}


def _clean_prog(tmp_path):
    ran = tmp_path / "ran"
    prog = tmp_path / "clean.sh"
    prog.write_text("#!/bin/sh\ntouch %s\ncat\n" % ran)
    prog.chmod(0o755)
    return ran, prog


def _change_a_file(repo, attributes):
    tracked = [p for p in repo.iterdir() if p.is_file() and p.name != ".gitattributes"][0]
    (repo / ".gitattributes").write_text(attributes)
    tracked.write_bytes(tracked.read_bytes() + b"x")


def test_a_filter_is_found_wherever_the_tree_s_config_keeps_it(make_firmware, tmp_path):
    """In a file the config includes, in a worktree's config, under a name with an `=` in
    it: each is the tree's own, and none of them runs."""
    ran, prog = _clean_prog(tmp_path)
    repo = make_firmware()
    (repo / ".git" / "more").write_text('[filter "inc"]\n\tclean = %s\n' % prog)
    gitrepo.run_git(repo, "config", "include.path", "more")
    gitrepo.run_git(repo, "config", "extensions.worktreeConfig", "true")
    gitrepo.run_git(repo, "config", "--worktree", "filter.wt.clean", str(prog))
    gitrepo.run_git(repo, "config", "filter.a=b.clean", str(prog))
    for name in ("inc", "wt", "a=b"):
        _change_a_file(repo, "* filter=%s\n" % name)
        assert gitrepo.is_dirty(repo) and gitrepo.describe(repo).endswith("-dirty")
        assert not ran.exists(), name


def test_a_submodule_s_filter_does_not_run(make_firmware, tmp_path):
    """`git status` looks into a submodule with the submodule's own config."""
    ran, prog = _clean_prog(tmp_path)
    repo = make_firmware()
    inner = make_firmware("inner")
    gitrepo.run_git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                    str(inner), "lib/inner")
    gitrepo.run_git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "sub")
    sub = repo / "lib" / "inner"
    gitrepo.run_git(sub, "config", "filter.x.clean", str(prog))
    _change_a_file(sub, "* filter=x\n")
    assert gitrepo.is_dirty(repo) and gitrepo.dirty_paths(repo)
    assert not ran.exists()
    # ...and the filter is live: git runs it when nothing stands in the way
    subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True)
    assert ran.exists()


def test_a_tree_git_would_read_somewhere_else_is_refused(make_firmware, tmp_path):
    """The filters are collected from the tree this walks. A submodule path that is a link
    to another repository, or a config that moves the worktree, has git read another."""
    ran, prog = _clean_prog(tmp_path)
    other = make_firmware("other")
    gitrepo.run_git(other, "config", "filter.x.clean", str(prog))
    _change_a_file(other, "* filter=x\n")
    repo = make_firmware()
    inner = make_firmware("inner")
    gitrepo.run_git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                    str(inner), "lib/inner")
    gitrepo.run_git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "sub")
    assert not gitrepo.is_dirty(repo / "lib")                   # from a directory inside it
    import shutil
    shutil.rmtree(repo / "lib" / "inner")
    (repo / "lib" / "inner").symlink_to(other)
    with pytest.raises(ProjectError, match="is a link"):
        gitrepo.is_dirty(repo)
    moved = make_firmware("moved")
    gitrepo.run_git(moved, "config", "core.worktree", str(other))
    with pytest.raises(ProjectError, match="another tree"):
        gitrepo.describe(moved)
    assert not ran.exists()
    assert gitrepo.dirty_paths(tmp_path) == []                  # no tree at all: nothing to read


def test_config_the_caller_s_environment_carries_is_kept(make_firmware, tmp_path, monkeypatch):
    """A CI passes `safe.directory` this way; the filter overrides go after it."""
    repo = make_firmware()
    gitrepo.run_git(repo, "config", "filter.x.clean", "false")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "someone")
    assert gitrepo.run_git(repo, "config", "user.name") == "someone"
    env = gitrepo._no_filters(repo)
    assert env["GIT_CONFIG_COUNT"] == "5" and env["GIT_CONFIG_KEY_1"] == "filter.x.clean"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "many")
    assert gitrepo._no_filters(repo)["GIT_CONFIG_COUNT"] == "4"

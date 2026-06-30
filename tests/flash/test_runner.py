"""The one side-effecting seam: turning subprocess outcomes into FlashError."""

from __future__ import annotations

import subprocess

import pytest

from openmv_ota.flash import runner
from openmv_ota.flash.errors import FlashError


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


def test_tolerate_fail_warns_and_continues(monkeypatch, capsys):
    def fake(argv, check):
        raise subprocess.CalledProcessError(74, argv)

    monkeypatch.setattr(runner.subprocess, "run", fake)
    runner.run(["dfu-util"], tolerate_fail=True)      # no raise -- the bootloader-write quirk
    assert "exited 74" in capsys.readouterr().err

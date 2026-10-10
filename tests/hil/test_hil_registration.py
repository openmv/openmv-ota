"""The HIL fleet registers its cameras the way a real desk does: the bench project's own desk key,
recorded on each run's (fresh) bench server, and `flash factory` pointed at that server."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "ci", "hil"))

import ota_cycle  # noqa: E402

from openmv_ota.project import desk_keys  # noqa: E402
from openmv_ota.project.passphrase import dev_passphrase_path  # noqa: E402


def _project(tmp_path, monkeypatch):
    p = dev_passphrase_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("dev-pass", encoding="utf-8")
    pushed = []
    monkeypatch.setattr(ota_cycle.subprocess, "run",
                        lambda argv, **kw: pushed.append((argv, kw["env"]["SSL_CERT_FILE"])))
    monkeypatch.setattr(ota_cycle, "log", lambda msg: None)
    monkeypatch.setitem(ota_cycle.CFG, "project", str(tmp_path))
    monkeypatch.setitem(ota_cycle.CFG, "server", "https://bench:8443")
    monkeypatch.setitem(ota_cycle.CFG, "token", "tok")
    monkeypatch.setitem(ota_cycle.CFG, "ca_node", "/tmp/ca.pem")
    return pushed


def test_flash_factory_registers_against_the_run_s_server(monkeypatch):
    monkeypatch.setitem(ota_cycle.CFG, "server", "https://bench:8443")
    monkeypatch.setitem(ota_cycle.CFG, "token", "tok")
    assert ota_cycle._register_args() == ("--server", "https://bench:8443", "--token", "tok")
    monkeypatch.setitem(ota_cycle.CFG, "server", "")          # the file legs: no server
    assert ota_cycle._register_args() == ("--no-register",)


def test_the_desk_key_is_made_once_renewed_when_lapsed_and_pushed_every_run(tmp_path,
                                                                            monkeypatch):
    pushed = _project(tmp_path, monkeypatch)
    ota_cycle._desk_ready()
    [entry] = desk_keys.read(tmp_path)
    assert entry.label == ota_cycle._DESK
    ota_cycle._desk_ready()                                    # still good: nothing new
    assert len(desk_keys.read(tmp_path)) == 1
    entry.expires = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    desk_keys._write(tmp_path, [entry])
    ota_cycle._desk_ready()                                    # lapsed: renewed, same key
    [renewed] = desk_keys.read(tmp_path)
    assert renewed.key_id == entry.key_id and renewed.expires > entry.expires
    assert len(pushed) == 3
    argv, ca = pushed[0]
    assert argv[-7:] == ["desk", "push", "--server", "https://bench:8443", "--token", "tok",
                         str(tmp_path)] and ca == "/tmp/ca.pem"

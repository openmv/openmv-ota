"""The project's keykeeper pin: it starts at [ota].keykeeper_pin, follows keykeeper's rotations
along the signed chain, and a build refuses to seal once the kept chain is a rotation behind."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openmv_ota.cli import main
from openmv_ota.keykeeper.ring import Ring
from openmv_ota.project import keykeeper_pins as kp
from openmv_ota.project.config import parse_config
from openmv_ota.project.errors import ProjectError

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def ring(tmp_path):
    r = Ring(tmp_path / "kk", "pass")
    r.init(NOW, period_days=30)
    return r


def test_sync_walks_from_the_configured_pin_and_keeps_the_chain(tmp_path, ring):
    root = tmp_path / "proj"
    gens = kp.sync(root, ring.genesis_pin(), ring.chain(), NOW)
    assert [g.key_id for g in gens] == [1, 2]
    assert kp.trusted_pin(root, "ignored once synced") == gens[0].pin()
    assert kp.sealing_generation(root, NOW).key_id == 1


def test_the_pin_follows_rotations_with_no_config_change(tmp_path, ring):
    root = tmp_path / "proj"
    kp.sync(root, ring.genesis_pin(), ring.chain(), NOW)
    later = NOW + timedelta(days=31)                 # generation 2 in effect, none announced
    with pytest.raises(ProjectError, match="rotated since this project last synced"):
        kp.sealing_generation(root, later)
    ring.extend(later, period_days=30)               # keykeeper announces generation 3
    kp.sync(root, "", ring.chain(), later)
    assert kp.trusted_pin(root, "").startswith("2:")
    assert kp.sealing_generation(root, later).key_id == 2


def test_a_chain_that_doesn_t_extend_the_pin_is_refused(tmp_path, ring):
    root = tmp_path / "proj"
    kp.sync(root, ring.genesis_pin(), ring.chain(), NOW)
    other = Ring(tmp_path / "rogue", "pass")
    other.init(NOW)
    with pytest.raises(ProjectError, match="doesn't verify from this project's pin"):
        kp.sync(root, "", other.chain(), NOW)
    with pytest.raises(ProjectError, match="no keykeeper pin"):
        kp.trusted_pin(tmp_path / "fresh", "")
    with pytest.raises(ProjectError, match="client keykeeper sync"):
        kp.sealing_generation(tmp_path / "fresh", NOW)


def test_a_hosted_project_starts_from_the_built_in_pin(tmp_path, ring):
    from openmv_ota.client.config import DEFAULT_SERVER_URL
    assert kp.trusted_pin(tmp_path / "fresh", "", DEFAULT_SERVER_URL + "/") == \
        kp.HOSTED_KEYKEEPER_PIN
    assert kp.trusted_pin(tmp_path / "fresh", "1:ab", DEFAULT_SERVER_URL) == "1:ab"   # config wins
    with pytest.raises(ProjectError, match="no keykeeper pin"):
        kp.trusted_pin(tmp_path / "fresh", "", "https://ota.example")    # self-hosted: set one
    with pytest.raises(ProjectError, match="doesn't verify from this project's pin"):
        kp.sync(tmp_path / "proj", "", ring.chain(), NOW, server_url=DEFAULT_SERVER_URL)


def test_a_damaged_file_says_so(tmp_path, ring):
    root = tmp_path / "proj"
    kp.sync(root, ring.genesis_pin(), ring.chain(), NOW)
    for text in ("[]", '{"pin": 1, "chain": []}'):
        kp.path_for(root).write_text(text, encoding="utf-8")
        with pytest.raises(ProjectError, match="damaged"):
            kp.trusted_pin(root, "")
    kp.path_for(root).write_text('{"pin": "1:00", "chain": []}', encoding="utf-8")
    with pytest.raises(ProjectError, match="isn't in the chain"):
        kp.sealing_generation(root, NOW)


def test_the_config_reads_the_switch_and_the_pin():
    base = '[targets]\nboards = ["OPENMV_N6"]\n[ota]\nenabled = true\n'
    cfg = parse_config(base, "p")
    assert not cfg.per_camera_encryption and cfg.keykeeper_pin == ""
    cfg = parse_config(base + 'per_camera_encryption = true\nkeykeeper_pin = "1:04ab"\n', "p")
    assert cfg.per_camera_encryption and cfg.keykeeper_pin == "1:04ab"


def test_the_sync_command(tmp_path, ring, monkeypatch, capsys):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "openmv-ota.toml").write_text(
        '[targets]\nboards = ["OPENMV_N6"]\n[ota]\nenabled = true\nkeykeeper_pin = "%s"\n'
        % ring.genesis_pin())

    class FakeApi:
        def __init__(self, cfg):
            assert (cfg.server_url, cfg.token) == ("https://ota.test", "tok")

        def keykeeper_chain(self):
            return ring.chain()

    monkeypatch.setattr("openmv_ota.client.api.Api", FakeApi)
    assert main(["project", "keys", "keykeeper", "sync", "--server", "https://ota.test",
                 "--token", "tok", str(root)]) == 0
    assert "generations 1-2" in capsys.readouterr().out
    assert kp.path_for(root).exists()

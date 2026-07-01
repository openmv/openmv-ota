"""The `server` optional-dependency guard."""

from __future__ import annotations

import pytest

from openmv_ota.server._extras import require_server_extra
from openmv_ota.server.errors import ServerError


def test_ok_when_importable():
    require_server_extra(_import=lambda name: object())      # no raise


def test_raises_install_hint_when_missing():
    def boom(name):
        raise ImportError(name)

    with pytest.raises(ServerError, match=r"pip install openmv-ota\[server\]") as e:
        require_server_extra(_import=boom)
    assert e.value.exit_code == 2

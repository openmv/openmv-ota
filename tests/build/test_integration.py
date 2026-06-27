"""End-to-end: publish a real golden -> new update with the build tools, then consume it
through the *device's own* installer code (manifest parse + select + the streaming delta
applier). Catches cross-tool contract drift the per-module unit tests can't: the manifest's
sha256/size vs the image, the delta's reconstruction vs the new image, and the device
selecting + applying exactly what the host produced.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
from pathlib import Path

from openmv_ota.build import romfs as build_mod


def _load_installer():
    src = (Path(__file__).resolve().parents[1].parent
           / "src/openmv_ota/build/device/openmv_ota/data/installer.py")
    spec = importlib.util.spec_from_file_location("openmv_ota._installer_e2e", str(src))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _SrcOf:
    """A read(n) over raw patch bytes, dribbled out, standing in for DeflateIO."""
    def __init__(self, data, step=13):
        self.data, self.pos, self.step = data, 0, step

    def read(self, n):
        n = min(n, self.step)
        out = self.data[self.pos:self.pos + n]
        self.pos += len(out)
        return out


# An incompressible ~200 KB asset, identical across versions, so the full image is large
# but the golden->new delta (only the version metadata changes) is tiny -> the delta wins.
_ASSET = b"".join(hashlib.sha256(bytes([i & 0xFF, (i >> 8) & 0xFF])).digest()
                  for i in range(6400))


def _build_version(root, app, repo, version):
    (app / "settings.json").write_text('{"app_version": "%s", "vendor": "Acme"}\n' % version)
    build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False, convert_models=False)
    [img] = build_mod.build_ota_image(root, firmware=repo)
    return gzip.decompress(img.output.read_bytes())


def test_publish_and_consume_end_to_end(make_project):
    from openmv_ota.ota.delta import apply_delta
    from openmv_ota.ota.keys import read_trusted_keys
    from openmv_ota.ota.manifest import parse_manifest, select_representation
    from openmv_ota.ota.verify import verify_manifest
    from openmv_ota.ota.version import encode_app_version
    from openmv_ota.project.project import ProjectPaths

    root, repo, app = make_project(
        boards=("OPENMV_N6",), ota=True,
        app_files={"main.py": "print('hi')\n", "data.bin": _ASSET,
                   "settings.json": '{"app_version": "1.0.0", "vendor": "Acme"}\n'})
    out = root / "build"

    golden_img = _build_version(root, app, repo, "1.0.0")
    (out / "golden.img").write_bytes(golden_img)
    new_img = _build_version(root, app, repo, "1.1.0")
    assert new_img != golden_img

    # --- publish: delta golden->new + a manifest that advertises both representations ---
    delta_path = out / "OPENMV_N6-v1.0.0-to-v1.1.0.delta.gz"
    dr = build_mod.build_delta(out / "golden.img", out / "OPENMV_N6-ota.img.gz", delta_path)
    [mres] = build_mod.build_manifest(
        root, url_base="https://dl.x.io/fw", firmware=repo, boards=["OPENMV_N6"],
        delta=delta_path, delta_base_version="1.0.0")
    manifest_bytes = mres.output.read_bytes()
    assert dr.gz_size < (out / "OPENMV_N6-ota.img.gz").stat().st_size   # delta beats full

    # --- host contract: the manifest is genuine and consistent with the image+delta ---
    trusted = read_trusted_keys(ProjectPaths(root).trusted_keys)
    ok, _reason = verify_manifest(manifest_bytes, trusted)
    assert ok
    body = parse_manifest(manifest_bytes).body
    assert body["sha256"] == hashlib.sha256(new_img).hexdigest()
    assert body["size"] == len(new_img)
    delta = gzip.decompress(delta_path.read_bytes())
    assert apply_delta(golden_img, delta) == new_img        # host applier reconstructs
    golden_pv = encode_app_version("1.0.0")
    assert select_representation(body, delta_capable=True,
                                golden_payload_version=golden_pv)["format"] == "ocdl"

    # --- device consume: the installer's own parse + select + streaming applier ---
    inst = _load_installer()
    m = inst._manifest_parse(manifest_bytes)               # device parser agrees with host
    assert m["body"]["sha256"] == body["sha256"]
    rep = inst._select_rep(m["body"], True, golden_pv)      # picks the delta (base matches)
    assert rep["format"] == "ocdl" and rep["url"] == "https://dl.x.io/fw/" + delta_path.name
    # reconstruct exactly as install() does: stream the patch against the golden BACK bytes
    gen = inst._delta_stream(inst._PatchReader(_SrcOf(delta)),
                             lambda o, n: golden_img[o:o + n], 4096)
    recon = b"".join(bytes(p) for p in gen)
    assert recon == new_img
    assert hashlib.sha256(recon).hexdigest() == body["sha256"]   # the install-time check

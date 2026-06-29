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
    from openmv_ota.ota import partition
    from openmv_ota.ota.keys import read_trusted_keys
    from openmv_ota.ota.manifest import parse_manifest
    from openmv_ota.ota.trailer import parse_trailer
    from openmv_ota.ota.verify import verify_manifest
    from openmv_ota.project import load_project
    from openmv_ota.project.project import ProjectPaths

    root, repo, app = make_project(
        boards=("OPENMV_N6",), ota=True,
        app_files={"main.py": "print('hi')\n", "data.bin": _ASSET,
                   "settings.json": '{"app_version": "1.0.0", "vendor": "Acme"}\n'})
    out = root / "build"
    t = load_project(root, firmware=repo).board("OPENMV_N6")

    # v1.0.0 is the *factory* golden -- its BACK slot is exactly what the device keeps.
    _build_version(root, app, repo, "1.0.0")
    build_mod.build_factory_romfs(root, firmware=repo, compile_py=False, convert_models=False)
    factory = (out / "OPENMV_N6-factory-romfs.img").read_bytes()
    device_back = factory[t.front_size:]                    # what the device reads as `old`

    # v1.1.0 release, published in one shot: image + delta(vs factory golden) + manifest,
    # relative URLs by default. This is the real `build ota-romfs --delta-from <factory>`.
    _build_version(root, app, repo, "1.1.0")
    [r] = build_mod.build_ota_romfs(root, firmware=repo,
                                    delta_from=out / "OPENMV_N6-factory-romfs.img")
    assert r.delta is not None
    new_img = gzip.decompress(r.image.read_bytes())
    assert r.delta.stat().st_size < r.image.stat().st_size  # delta beats the full download

    # --- host contract: the manifest is genuine and consistent with the image ---
    manifest_bytes = r.manifest.read_bytes()
    ok, _reason = verify_manifest(manifest_bytes, read_trusted_keys(ProjectPaths(root).trusted_keys))
    assert ok
    body = parse_manifest(manifest_bytes).body
    assert body["sha256"] == hashlib.sha256(new_img).hexdigest()
    assert body["representations"][0]["url"] == r.image.name   # relative (host-portable)

    # the delta's base is the factory golden version, read from its BACK trailer
    back_tr = next(tr for lbl, _b, tr in partition.slots(factory) if lbl == "BACK")
    golden_pv = parse_trailer(back_tr).payload_version
    ocdl = next(rep for rep in body["representations"] if rep["format"] == "ocdl")
    assert ocdl["base_payload_version"] == golden_pv

    # --- device consume: the installer's own parse + select + streaming applier ---
    inst = _load_installer()
    m = inst._manifest_parse(manifest_bytes)
    rep = inst._select_rep(m["body"], True, golden_pv)      # delta picked (base matches golden)
    assert rep["format"] == "ocdl"
    # the relative URL resolves against the manifest's own URL
    assert (inst._resolve_url("https://dl.x.io/fw/OPENMV_N6-manifest.bin", rep["url"])
            == "https://dl.x.io/fw/" + r.delta.name)
    # reconstruct exactly as install() does: stream the patch against the REAL device BACK
    # bytes (factory back slot, confirmed status) -- the masking-free check.
    delta = gzip.decompress(r.delta.read_bytes())
    gen = inst._delta_stream(inst._PatchReader(_SrcOf(delta)),
                             lambda o, n: device_back[o:o + n], 4096)
    recon = b"".join(bytes(p) for p in gen)
    assert recon == new_img
    assert hashlib.sha256(recon).hexdigest() == body["sha256"]   # the install-time check

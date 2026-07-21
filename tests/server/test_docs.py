"""The human-readable API reference: / -> /docs (self-hosted ReDoc) + OpenAPI extras."""

from fastapi.testclient import TestClient

from openmv_ota.server.app import create_app
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage


def _app(tmp_path, *, base_url="https://ota.test"):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("cohort_salt", "test-secret")
    storage = LocalArtifactStorage(str(tmp_path / "blobs"))
    settings = ServerSettings(base_url=base_url, swd_ids_verify_url="u",
                              swd_ids_verify_token="t")
    return create_app(settings, storage=storage, metastore=store)


def test_root_redirects_to_docs(tmp_path):
    client = TestClient(_app(tmp_path))
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/docs"


def test_docs_page_serves_redoc(tmp_path):
    client = TestClient(_app(tmp_path))
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-cache"  # deploys must show current HTML
    assert "Redoc.init('/openapi.json'" in resp.text
    assert "/docs/redoc.standalone.js" in resp.text
    assert 'class="topbar-logo" src="/docs/logo.png"' in resp.text  # full wordmark in the bar
    # OpenMV's standard theming: shared storage key, Auto/Light/Dark button, white
    # logo on dark, and a dark ReDoc theme.
    assert "theme-preference" in resp.text
    assert 'class="theme-toggle"' in resp.text
    assert "/docs/logo-dark.png" in resp.text
    # The standard globe language switcher is present (English-only for now).
    assert 'class="theme-toggle lang-toggle"' in resp.text
    assert 'data-lang="en" aria-current="true"' in resp.text
    assert 'sidebar: { backgroundColor: "#1e293b"' in resp.text
    # Intro prose sections are tightened without touching endpoint spacing.
    assert 'div[id^="section/"] { padding-top: 16px' in resp.text
    # The toggle lives in a fixed header bar (never floats over content); ReDoc's
    # sticky elements are offset below it via scrollYOffset.
    assert 'class="topbar"' in resp.text
    assert "scrollYOffset: 52" in resp.text
    assert "padding-top: 3.25rem" in resp.text


def test_docs_assets_are_served(tmp_path):
    client = TestClient(_app(tmp_path))
    js = client.get("/docs/redoc.standalone.js")
    assert js.status_code == 200
    assert js.headers["content-type"].startswith("text/javascript")
    for path in ("/docs/logo.png", "/docs/logo-dark.png"):
        logo = client.get(path)
        assert logo.status_code == 200
        assert logo.headers["content-type"] == "image/png"
        assert logo.content[:8] == b"\x89PNG\r\n\x1a\n"
    fav = client.get("/favicon.ico")  # the square aperture icon mark, not the wordmark
    assert fav.status_code == 200
    assert fav.headers["content-type"] == "image/x-icon"
    assert fav.content[:4] == b"\x00\x00\x01\x00"


def test_openapi_has_branding_and_server_url(tmp_path):
    client = TestClient(_app(tmp_path))
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "OpenMV OTA Update Server"
    assert "x-logo" not in schema["info"]  # the header bar carries the wordmark instead
    assert schema["servers"] == [{"url": "https://ota.test"}]
    assert {t["name"] for t in schema["tags"]} == {"Device API", "Admin", "Publishing",
                                                   "Health"}
    # Every documented operation is tagged (the docs routes themselves are excluded).
    for path, ops in schema["paths"].items():
        for op in ops.values():
            assert op["tags"], "untagged operation: %s" % path


def test_openapi_without_base_url_has_no_servers(tmp_path):
    client = TestClient(_app(tmp_path, base_url=""))
    schema = client.get("/openapi.json").json()
    assert "servers" not in schema


def test_openapi_schema_is_cached(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    first = client.get("/openapi.json").json()
    assert client.get("/openapi.json").json() == first

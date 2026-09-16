"""The human-readable API reference: / -> /docs (self-hosted ReDoc) + OpenAPI extras."""

from fastapi.testclient import TestClient

from openmv_ota.server.app import create_app
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.storage import LocalArtifactStorage


def _app(tmp_path, *, base_url="https://ota.test"):
    store = SqliteMetadataStore(str(tmp_path / "ota.db"))
    store.migrate()
    store.set_meta("capability_secret", "test-secret")
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


def test_openapi_declares_the_bearer_scheme_and_every_route_that_needs_it(tmp_path):
    """`require_scope` reads the Authorization header itself, so FastAPI infers no
    security and a client generated from the schema would send no credentials at all.
    The hook adds it back: every guarded operation carries bearerAuth and names the
    scope it needs, and the handful that take no token stay open on purpose."""
    schema = TestClient(_app(tmp_path)).get("/openapi.json").json()
    scheme = schema["components"]["securitySchemes"]["bearerAuth"]
    assert scheme["type"] == "http" and scheme["scheme"] == "bearer"
    assert "publish" in scheme["description"] and "404" in scheme["description"]

    open_ops, guarded = set(), {}
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            if "security" in op:
                assert op["security"] == [{"bearerAuth": []}], path
                assert "**Requires scope:**" in op["description"], path
                guarded[(method, path)] = op["description"]
            else:
                open_ops.add((method.upper(), path))
    # Open by design: liveness, the two device endpoints (gated by registration and
    # rate limit, never by an account credential), and the capability download URL.
    assert open_ops == {("GET", "/healthz"), ("POST", "/api/v1/check"),
                        ("POST", "/api/v1/feedback"), ("GET", "/d/{token}/{filename}")}
    assert len(guarded) == 44
    assert "`observe`" in guarded[("get", "/api/v1/admin/devices")]
    assert "`publish`" in guarded[("post", "/api/v1/admin/releases")]
    assert "`accounts`" in guarded[("post", "/api/v1/admin/accounts")]


def test_every_operation_says_what_it_does(tmp_path):
    """A reference with bare endpoints is not a reference: 19 operations once showed a
    name and a parameter list and nothing else, including publish, rollout create and
    the device list. Descriptions come from the handler docstrings, so this fails the
    moment a route is added without one."""
    schema = TestClient(_app(tmp_path)).get("/openapi.json").json()
    bare = [f"{m.upper()} {p}" for p, ops in schema["paths"].items() for m, op in ops.items()
            if len((op.get("description") or "").replace("**Requires scope:**", "").strip()) < 40]
    assert not bare, "operations with no real description: %s" % bare


def test_the_reference_explains_how_the_objects_map(tmp_path):
    """An integrator's first questions are what a product is, where its id comes from,
    and how a device ends up in their account -- none of which are visible from the
    endpoint list, because none of them are API calls."""
    schema = TestClient(_app(tmp_path)).get("/openapi.json").json()
    desc = schema["info"]["description"]
    assert 'sha256("<product>:<board>")' in desc         # ids are computed, not assigned
    assert "63 bits" in desc                             # and wide enough not to collide
    assert "two products" in desc                        # one line, two boards
    assert "no \"create product\" call" in desc
    assert "learns" in desc and "sticky" in desc         # how a device joins an account
    assert "list of products" in desc                    # the per-customer credential
    assert "product_id_str" in desc and "2^53" in desc   # and the JS precision trap
    assert "404" in desc                                 # and how it answers outside them
    assert "filter-aware `total`" in desc                # how to page

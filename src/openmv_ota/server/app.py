"""The FastAPI application factory + the device-facing API.

``create_app(settings, *, storage, metastore, verifier, admin_auth)`` is the embeddable core:
every collaborator is injectable (OpenMV's website supplies its own), defaulting to the
settings-driven backends for self-hosters. The device endpoints:

* ``POST /api/v1/check`` -- rate-limit (per IP) -> **registration verify** (unregistered leaves
  zero footprint) -> device-registry upsert -> rollout decision -> a capability manifest URL.
* ``GET /d/{token}/{filename}`` -- the capability gateway: validate the token, then 302-redirect
  to a presigned artifact URL (s3) or stream it (local). One token guards the whole bundle.
* ``GET /healthz``.

Routes live at module scope (not closed over ``create_app``) so FastAPI can resolve their type
hints under ``from __future__ import annotations``; per-request collaborators come off
``request.app.state``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from openmv_ota import __version__

from . import capability
from . import datalog as datalog_mod
from . import live as live_mod
from .auth import TokenAuth
from .boardmap import swd_ids_board_code
from .errors import ServerError
from .metastore import build_metastore
from .ratelimit import RateLimiter
from .rollout import offers_update, should_autopause
from .storage import build_storage
from .verify import build_verifier

router = APIRouter()

_MEDIA = {"manifest.bin": "application/octet-stream"}

# --- human-readable API reference (ReDoc, self-hosted) --------------------------------------

_DOCS_DIR = Path(__file__).parent / "docs_static"

_API_DESCRIPTION = """\
The OpenMV OTA update server delivers **signed over-the-air updates** to OpenMV
cameras. Update bundles are signed at build time and verified on-device; the
server distributes them and manages rollouts, and never holds signing keys.

## Device flow

A camera checks in at `POST /api/v1/check`; if an update is offered it downloads
the bundle over a short-lived link, then reports the result at
`POST /api/v1/feedback`.

## Authentication

* **Device endpoints** are rate-limited and gated by device registration.
* **Admin and publishing endpoints** use `Authorization: Bearer <token>`; tokens
  belong to an account and carry scopes (`publish`, `manage`, `observe`,
  `accounts`), and every operation is scoped to the token's account.

Self-hosting and operations are covered in the
[server manual](https://github.com/openmv/openmv-ota/blob/main/docs/server.md).
"""

_OPENAPI_TAGS = [
    {"name": "Device API",
     "description": "Called by cameras in the field: check-in, capability-URL downloads, "
                    "and install feedback."},
    {"name": "Admin",
     "description": "Account-scoped management of accounts, tokens, rollouts, cohorts, and "
                    "devices. Bearer-token auth with scopes."},
    {"name": "Publishing",
     "description": "Upload and publish signed release bundles built by `openmv-ota build`."},
    {"name": "Health", "description": "Liveness probe."},
]

# The docs page carries OpenMV's standard theme toggle (same button graphics, same
# Auto -> Light -> Dark cycle, same `theme-preference` localStorage key as the other
# OpenMV sites). ReDoc takes its colors at init time, so the toggle re-inits ReDoc
# with the matching theme object; the logo swaps to the white variant on dark.
_REDOC_HTML = """<!DOCTYPE html>
<html>
<head>
<title>OpenMV OTA Update Server — API Reference</title>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="color-scheme" content="light dark"/>
<link rel="icon" type="image/x-icon" href="/favicon.ico"/>
<script>
/* Apply theme before first paint to avoid a flash of the wrong theme. */
(function () {
  try {
    var pref = localStorage.getItem('theme-preference') || 'auto';
    var resolved = pref === 'auto'
      ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : pref;
    document.documentElement.dataset.theme = resolved;
    document.documentElement.dataset.themePref = pref;
  } catch (e) {
    document.documentElement.dataset.theme = 'light';
    document.documentElement.dataset.themePref = 'auto';
  }
})();
</script>
<style>
  body { margin: 0; padding: 0; }
  :root[data-theme="dark"] body { background: #0f172a; }
  :root[data-theme="dark"] .menu-content input { color: #e7eefb; }
  /* Schema header labels ("REQUEST BODY SCHEMA:", the content-type chip) keep
     ReDoc's default dark ink regardless of theme — lift them in dark mode. */
  :root[data-theme="dark"] .api-content h5,
  :root[data-theme="dark"] .api-content h5 span { color: #9fb8d8; }
  /* Sample tabs: ReDoc's selected tab is white-on-anything by default. Restyle
     for dark; response tabs (.tab-success/.tab-error) keep their status colors. */
  :root[data-theme="dark"] .react-tabs__tab--selected {
    background: #0f172a;
    border: 1px solid #475569;
  }
  :root[data-theme="dark"] .react-tabs__tab--selected:not(.tab-success):not(.tab-error) {
    color: #e7eefb;
  }
  :root[data-theme="dark"] .react-tabs__tab--selected.tab-success { color: #34d399; }
  :root[data-theme="dark"] .react-tabs__tab--selected.tab-error { color: #f87171; }
  /* Deep-link anchor icon in section/tag headings: ReDoc's default (~#9E9EFF)
     is too low-contrast on the dark slate -- use the theme accent. */
  :root[data-theme="dark"] .api-content h1 > a,
  :root[data-theme="dark"] .api-content h2 > a,
  :root[data-theme="dark"] .api-content h3 > a { color: #60a5fa; }
  /* Tighten only the intro prose: the markdown sections of info.description get
     `section/` ids, and the title/description block is .api-content's first child
     (40px bottom padding by default). The endpoint groups (`tag/` ids) keep
     ReDoc's default spacing. */
  div[id^="section/"] { padding-top: 16px; padding-bottom: 16px; }
  .api-content > div:first-child { padding-bottom: 16px; }

  /* Slim fixed header bar: the toggle lives here (never floats over content).
     ReDoc is told about it via scrollYOffset, so its sticky sidebar and
     endpoint headers start below the bar. */
  .topbar {
    position: fixed; top: 0; left: 0; right: 0; height: 3.25rem; z-index: 1000;
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 1rem; box-sizing: border-box;
    background: #ffffff; border-bottom: 1px solid #d1d5db;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  :root[data-theme="dark"] .topbar { background: #1e293b; border-bottom-color: #475569; }
  .topbar-title {
    display: flex; align-items: center; gap: 0.75rem;
    color: #6b7280; font-size: 0.875rem; font-weight: 600;
  }
  :root[data-theme="dark"] .topbar-title { color: #94a3b8; }
  /* The full wordmark: blue in light mode, swapped to the white variant in dark. */
  .topbar-logo { height: 1.75rem; width: auto; display: block; }
  :root[data-theme="dark"] img.topbar-logo { content: url("/docs/logo-dark.png"); }
  /* Nudge the label down so it sits on the wordmark's visual baseline. */
  .topbar-label { margin-top: 0.3rem; }
  #redoc-container { padding-top: 3.25rem; }

  .theme-toggle {
    display: inline-flex; align-items: center; justify-content: center;
    width: 2.25rem; height: 2.25rem; padding: 0; flex-shrink: 0;
    background: transparent; border: 1px solid #d1d5db; border-radius: 50%;
    color: #6b7280; cursor: pointer;
  }
  .theme-toggle:hover { color: #111827; border-color: #9ca3af; }
  :root[data-theme="dark"] .theme-toggle { border-color: #475569; color: #94a3b8; }
  :root[data-theme="dark"] .theme-toggle:hover { color: #f1f5f9; border-color: #64748b; }
  .theme-toggle svg { width: 1rem; height: 1rem; display: none; }
  :root[data-theme-pref="auto"]  .theme-toggle .icon-auto,
  :root[data-theme-pref="light"] .theme-toggle .icon-light,
  :root[data-theme-pref="dark"]  .theme-toggle .icon-dark { display: block; }

  /* Language switcher: the globe borrows .theme-toggle's button styling (it has
     no icon-auto/light/dark class, so un-hide its svg). English-only for now —
     the menu is the standard rail for future translations. */
  .topbar-controls { display: flex; align-items: center; gap: 0.5rem; }
  .lang-toggle svg { display: block; }
  .lang-switcher { position: relative; }
  .lang-menu {
    position: absolute; top: calc(100% + 0.375rem); right: 0; z-index: 1100;
    min-width: 11rem; max-height: 70vh; overflow-y: auto;
    margin: 0; padding: 0.25rem; list-style: none;
    background: #ffffff; border: 1px solid #d1d5db; border-radius: 0.5rem;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
  }
  :root[data-theme="dark"] .lang-menu {
    background: #1e293b; border-color: #475569; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
  }
  .lang-menu[hidden] { display: none; }
  .lang-menu li { margin: 0; padding: 0; }
  .lang-option {
    display: block; width: 100%; margin: 0; padding: 0.375rem 0.75rem;
    font-size: 0.875rem; font-family: inherit; text-align: left;
    background: transparent; border: none; border-radius: 0.375rem;
    color: #111827; cursor: pointer;
  }
  :root[data-theme="dark"] .lang-option { color: #f1f5f9; }
  .lang-option:hover { background: rgba(0, 0, 0, 0.04); }
  :root[data-theme="dark"] .lang-option:hover { background: rgba(255, 255, 255, 0.06); }
  .lang-option[aria-current="true"] { font-weight: 600; color: #2563eb; }
  :root[data-theme="dark"] .lang-option[aria-current="true"] { color: #60a5fa; }
</style>
</head>
<body>
<div class="topbar">
<span class="topbar-title"><img class="topbar-logo" src="/docs/logo.png" alt="OpenMV"> <span class="topbar-label">API Reference</span></span>
<div class="topbar-controls">
<div class="lang-switcher">
<button type="button" class="theme-toggle lang-toggle" aria-haspopup="true" aria-expanded="false" aria-label="Language" title="Language">
<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
</button>
<ul class="lang-menu" role="menu" hidden>
<li role="none"><button type="button" role="menuitem" class="lang-option" data-lang="en" aria-current="true">English</button></li>
</ul>
</div>
<button type="button" class="theme-toggle" aria-label="Toggle theme" title="Theme">
<svg class="icon-auto"  width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="4" width="20" height="14" rx="2"/><line x1="8" y1="20" x2="16" y2="20"/><line x1="12" y1="16" x2="12" y2="20"/></svg>
<svg class="icon-light" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="22"/><line x1="2" y1="12" x2="5" y2="12"/><line x1="19" y1="12" x2="22" y2="12"/><line x1="4.93" y1="4.93" x2="7.05" y2="7.05"/><line x1="16.95" y1="16.95" x2="19.07" y2="19.07"/><line x1="4.93" y1="19.07" x2="7.05" y2="16.95"/><line x1="16.95" y1="7.05" x2="19.07" y2="4.93"/></svg>
<svg class="icon-dark"  width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
</button>
</div>
</div>
<div id="redoc-container"></div>
<script src="/docs/redoc.standalone.js"></script>
<script>
(function () {
  var LIGHT = {};
  var DARK = {
    colors: {
      primary: { main: "#60a5fa" },
      success: { main: "#34d399" },
      error: { main: "#f87171" },
      warning: { main: "#fbbf24" },
      text: { primary: "#e7eefb", secondary: "#9fb8d8" },
      border: { dark: "#334155", light: "#1e293b" }
    },
    sidebar: { backgroundColor: "#1e293b", textColor: "#e7eefb" },
    rightPanel: { backgroundColor: "#1e293b" },
    schema: { nestedBackground: "#273449", typeNameColor: "#9fb8d8",
              typeTitleColor: "#9fb8d8" },
    typography: { code: { backgroundColor: "#1e293b", color: "#e7eefb" } }
  };
  var media = window.matchMedia('(prefers-color-scheme: dark)');

  function resolve(pref) { return pref === 'auto' ? (media.matches ? 'dark' : 'light') : pref; }

  function render() {
    var dark = document.documentElement.dataset.theme === 'dark';
    Redoc.init('/openapi.json', { scrollYOffset: 52, theme: dark ? DARK : LIGHT },
               document.getElementById('redoc-container'));
  }

  function apply(pref) {
    document.documentElement.dataset.themePref = pref;
    document.documentElement.dataset.theme = resolve(pref);
    render();
  }

  // The globe borrows .theme-toggle styling but is not a theme control.
  document.querySelector('.theme-toggle:not(.lang-toggle)').addEventListener('click', function () {
    var order = ['auto', 'light', 'dark'], current;
    try { current = localStorage.getItem('theme-preference') || 'auto'; } catch (e) { current = 'auto'; }
    var next = order[(order.indexOf(current) + 1) % order.length];
    try { localStorage.setItem('theme-preference', next); } catch (e) {}
    apply(next);
  });

  // Language switcher (same behavior as OpenMV's other sites: cookie + reload).
  var langBtn = document.querySelector('.lang-toggle');
  var langMenu = document.querySelector('.lang-menu');
  function closeLangMenu() { langMenu.hidden = true; langBtn.setAttribute('aria-expanded', 'false'); }
  langBtn.addEventListener('click', function (e) {
    e.stopPropagation();
    var willOpen = langMenu.hidden;
    langMenu.hidden = !willOpen;
    langBtn.setAttribute('aria-expanded', String(willOpen));
  });
  langMenu.querySelectorAll('.lang-option').forEach(function (opt) {
    opt.addEventListener('click', function () {
      var code = opt.getAttribute('data-lang');
      if (!code) return;
      document.cookie = 'lang=' + encodeURIComponent(code) + ';path=/;max-age=31536000;samesite=Lax';
      location.reload();
    });
  });
  document.addEventListener('click', function () { if (!langMenu.hidden) closeLangMenu(); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !langMenu.hidden) closeLangMenu();
  });

  if (media.addEventListener) {
    media.addEventListener('change', function () {
      var pref;
      try { pref = localStorage.getItem('theme-preference') || 'auto'; } catch (e) { pref = 'auto'; }
      if (pref === 'auto') apply(pref);
    });
  }

  render();
})();
</script>
</body>
</html>
"""


@router.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@router.get("/docs", include_in_schema=False)
def docs_page():
    # no-cache: revalidate on every visit so deploys (and reviews) show current HTML;
    # the heavy assets (redoc bundle, logos) remain cacheable.
    return HTMLResponse(_REDOC_HTML, headers={"Cache-Control": "no-cache"})


@router.get("/docs/redoc.standalone.js", include_in_schema=False)
def docs_js():
    return FileResponse(_DOCS_DIR / "redoc.standalone.js", media_type="text/javascript")


@router.get("/docs/logo.png", include_in_schema=False)
def docs_logo():
    return FileResponse(_DOCS_DIR / "logo.png", media_type="image/png")


@router.get("/docs/logo-dark.png", include_in_schema=False)
def docs_logo_dark():
    return FileResponse(_DOCS_DIR / "logo-dark.png", media_type="image/png")


@router.get("/favicon.ico", include_in_schema=False)
def favicon():  # the OpenMV aperture icon mark (browsers request this path by default)
    return FileResponse(_DOCS_DIR / "favicon.ico", media_type="image/x-icon")


class CheckIn(BaseModel):
    device_id: str
    product_id: int                          # the release<->device join key (from identity())
    account_id: str = ""                     # the maker's account (from identity()); '' = self-host
    board: str | None = None               # camera model, display-only
    product: str | None = None
    app_version: str | None = None
    payload_version: int = 0
    slot: str | None = None
    representation: str | None = None
    fallback_reason: str | None = None
    confirmed: bool = False
    streams: list[str] = []                  # live image stream names (multi-camera boards);
    #                                          empty -> the default single stream


class Feedback(BaseModel):
    device_id: str
    product_id: int
    account_id: str = ""                     # the maker's account (from identity()); '' = self-host
    board: str | None = None               # firmware board name (for the registration gate)
    release_id: str
    status: str                            # terminal outcome: 'installed' | 'failed'
    reason: str | None = None


def _media_type(filename: str) -> str:
    return _MEDIA.get(filename, "application/gzip")


def _artifact_key(release: dict, filename: str) -> str | None:
    """The storage key for the artifact named ``filename`` within ``release`` (or None)."""
    if filename == "manifest.bin":
        return release["manifest_key"]
    for rep in release["representations"]:
        if rep["url"] == filename:
            return release["image_key"] if rep["format"] == "full" else release["delta_key"]
    return None


def _offer(state, rel):
    """Mint a capability manifest URL for a release."""
    token = capability.mint(state.secret, rel["release_id"], ttl=state.settings.capability_ttl)
    return "%s/d/%s/manifest.bin" % (state.settings.base_url.rstrip("/"), token)


def _decide(state, checkin, cohort, existing=None, account_id=""):
    """The release to offer this device (a pin overrides the rollout) and whether to offer it.
    ``account_id`` is the device's *effective* account (its sticky binding, not the raw report).
    Returns ``(rollout | None, release | None, offered: bool, manifest_url | None)``."""
    ms = state.metastore
    # A pin (device wins over cohort) overrides the rollout: offer the pinned release iff it's an
    # upgrade -- a pin to the current/older version just holds the device (no rollout reaches it).
    pinned = (existing["pinned_release_id"] if existing else None) \
        or ms.get_cohort_pin(checkin.product_id, cohort, account_id=account_id)
    if pinned:
        rel = ms.get_release(pinned)
        # a release is only ever offered to a device of its own account (defense in depth behind
        # the admin-side pin check): a cross-account or missing/older pin just holds the device.
        if (rel is None or rel["account_id"] != account_id
                or rel["payload_version"] <= checkin.payload_version):
            return None, rel, False, None
        return None, rel, True, _offer(state, rel)
    ro = ms.active_rollout(checkin.product_id, cohort, account_id=account_id)
    if ro is None:
        return None, None, False, None
    rel = ms.get_release(ro["release_id"])
    if rel is None:
        return ro, None, False, None
    offered = offers_update(
        current_payload_version=checkin.payload_version,
        release_payload_version=rel["payload_version"], rollout_state=ro["state"],
        rollout_percent=ro["percent"], rollout_id=ro["rollout_id"], device_id=checkin.device_id)
    if not offered:
        return ro, rel, False, None
    return ro, rel, True, _offer(state, rel)


def _account(ms, ro, rel, checkin, existing, offered):
    """Feed the rollout's counters from this check-in + auto-pause on the failure threshold."""
    if ro is None or rel is None:
        return
    rid = ro["rollout_id"]
    prev_offered = existing["last_offered_release_id"] if existing else None
    prev_fallback = existing["fallback_reason"] if existing else None
    prev_pv = existing["current_payload_version"] if existing else None
    if offered and prev_offered != rel["release_id"]:            # newly entering this rollout
        ms.bump_rollout(rid, attempted=1)
    # a device we offered this release, now *transitioning to running it* -> a success
    if (prev_offered == rel["release_id"] and prev_pv != rel["payload_version"]
            and checkin.payload_version == rel["payload_version"]):
        ms.bump_rollout(rid, updated=1)
    # a device we offered this release, transitioning *into* a fallback -> one failure
    if prev_offered == rel["release_id"] and checkin.fallback_reason and not prev_fallback:
        ms.bump_rollout(rid, failures=1)
        fresh = ms.get_rollout(rid)
        if fresh["state"] == "active" and should_autopause(
                fresh["failures"], fresh["attempted"], fresh["failure_threshold"]):
            ms.update_rollout(rid, state="paused")
            ms.append_audit(actor="system", action="rollout.autopause", entity_type="rollout",
                            entity_id=rid, account_id=ro.get("account_id", ""),
                            data={"failures": fresh["failures"], "attempted": fresh["attempted"]})


def _verify(state, req):
    """Translate the firmware board name to the swd-ids code, then the registration check.
    ``req`` is a CheckIn or Feedback (both carry ``board`` + ``device_id``)."""
    swd_board = swd_ids_board_code(req.board, state.settings.board_code_overrides)
    return state.verifier.verify(swd_board, req.device_id)


def _effective_account(ms, checkin):
    """The device's authoritative account for scoping. A binding (learned on the first valid
    check-in, or an admin override) is **sticky** and wins -- so a later golden fallback reporting
    a different/empty account can't strand the device. Absent a binding, the first non-empty report
    is *learned* (and returned); a device that has only ever reported '' stays in the '' account.
    Only reached for a registered device, so no unregistered id can create a binding."""
    bound = ms.device_account(checkin.device_id)
    if bound is not None:
        return bound["account_id"]
    if checkin.account_id:
        ms.bind_device_account(checkin.device_id, checkin.account_id, source="learned")
        return checkin.account_id
    return ""


@router.get("/healthz", tags=["Health"])
def healthz():
    return {"ok": True}


@router.post("/api/v1/check", tags=["Device API"])
def check(checkin: CheckIn, request: Request):
    st = request.app.state
    nothing = {"update": False, "poll_after_s": st.settings.poll_after_s}
    ip = request.client.host if request.client else "-"
    if not st.ratelimit.allow(ip):
        return JSONResponse(nothing, status_code=429,
                            headers={"Retry-After": str(st.settings.poll_after_s)})

    if checkin.board in st.settings.unverified_boards:
        # A board type swd-ids never registers (legacy Arduino, pre-registration M4). Serve OTA
        # READ-ONLY: skip the registration check AND the device-registry write, so a fake id can't
        # grow the DB -- zero footprint preserved, at the cost of no fleet tracking for these boards.
        _, rel, offered, manifest_url = _decide(st, checkin, "__default__")
        if manifest_url:
            return {"update": True, "manifest_url": manifest_url,
                    "release_id": rel["release_id"], "poll_after_s": st.settings.poll_after_s}
        return nothing

    reg = _verify(st, checkin)
    if not reg.registered:
        return nothing                                          # ZERO footprint for unregistered ids
    ms = st.metastore
    account_id = _effective_account(ms, checkin)                # sticky binding, not the raw report
    existing = ms.get_device(checkin.device_id)
    cohort = existing["cohort"] if existing else "__default__"
    ro, rel, offered, manifest_url = _decide(st, checkin, cohort, existing, account_id)
    _account(ms, ro, rel, checkin, existing, offered)
    release_id = rel["release_id"] if offered else None
    ms.upsert_device(
        device_id=checkin.device_id, product_id=checkin.product_id, board=checkin.board, cohort=cohort,
        current_version=checkin.app_version, current_payload_version=checkin.payload_version,
        slot=checkin.slot, representation=checkin.representation,
        fallback_reason=checkin.fallback_reason, confirmed=1 if checkin.confirmed else 0,
        last_offered_release_id=release_id, registrar_ref=reg.registrar_ref or None,
        account_id=account_id)
    if manifest_url:
        resp = {"update": True, "manifest_url": manifest_url, "release_id": release_id,
                "poll_after_s": st.settings.poll_after_s}
    else:
        resp = dict(nothing)
    # OpenMV Live: registered devices get a fresh camera grant each check-in (the
    # unverified-board bypass above never reaches here -- Live requires registration).
    grant = live_mod.camera_grant(st.settings, checkin.device_id, checkin.streams)
    if grant is not None:
        resp["live"] = grant
    # Datalake: registered devices get a fresh ingest grant each check-in too.
    ingest = datalog_mod.ingest_grant(st.settings, account_id, checkin.device_id)
    if ingest is not None:
        resp["ingest"] = ingest
    return resp


@router.post("/api/v1/feedback", tags=["Device API"])
def feedback(report: Feedback, request: Request):
    """Explicit terminal outcome of an offered update (precise success/failure vs. inferring it
    from the next check-in). Recorded ONLY for a registered device -- an unregistered or bypassed
    board is a no-op, so this stays zero-footprint too."""
    st = request.app.state
    ip = request.client.host if request.client else "-"
    if not st.ratelimit.allow(ip):
        return JSONResponse({"ok": False}, status_code=429,
                            headers={"Retry-After": str(st.settings.poll_after_s)})
    if report.status not in ("installed", "failed"):
        raise HTTPException(status_code=400, detail="status must be 'installed' or 'failed'")
    if report.board in st.settings.unverified_boards or not _verify(st, report).registered:
        return {"ok": False}                                    # untracked / unregistered -> no write
    st.metastore.record_deployment(
        device_id=report.device_id, release_id=report.release_id, product_id=report.product_id,
        status=report.status, reason=report.reason, account_id=report.account_id)
    return {"ok": True}


@router.get("/d/{token}/{filename}", tags=["Device API"])
def artifact(token: str, filename: str, request: Request):
    st = request.app.state
    release_id = capability.verify(st.secret, token)
    if release_id is None:
        raise HTTPException(status_code=404)
    rel = st.metastore.get_release(release_id)
    key = _artifact_key(rel, filename) if rel is not None else None
    if key is None:
        raise HTTPException(status_code=404)
    url = st.storage.url_for(key)
    if url is not None:
        return RedirectResponse(url, status_code=302)           # offload to object storage
    try:
        data = st.storage.get(key)
    except ServerError:
        raise HTTPException(status_code=404) from None
    return Response(content=data, media_type=_media_type(filename))


def create_app(settings, *, storage=None, metastore=None, verifier=None, admin_auth=None):
    """Build the ASGI app. Collaborators default to the settings-driven backends; the website
    injects its own. The server HMAC secret comes from the DB (seeded by ``server init``) or
    ``OPENMV_OTA_COHORT_SALT`` -- required so capability tokens are stable across workers."""
    storage = storage if storage is not None else build_storage(settings)
    metastore = metastore if metastore is not None else build_metastore(settings)
    verifier = verifier if verifier is not None else build_verifier(settings)
    secret = metastore.get_meta("cohort_salt") or settings.cohort_salt
    if not secret:
        raise ServerError("no server secret -- run `server init` or set OPENMV_OTA_COHORT_SALT",
                          exit_code=2)

    app = FastAPI(title="OpenMV OTA Update Server", version=__version__,
                  description=_API_DESCRIPTION, openapi_tags=_OPENAPI_TAGS,
                  docs_url=None, redoc_url=None)  # /docs is our self-hosted ReDoc page
    app.state.settings = settings
    app.state.storage = storage
    app.state.metastore = metastore
    app.state.verifier = verifier
    app.state.admin_auth = admin_auth if admin_auth is not None else TokenAuth(metastore)
    app.state.secret = secret
    app.state.ratelimit = RateLimiter(settings.checkin_rate_per_min)
    app.include_router(router)
    from .admin import admin
    from .publish import publish
    app.include_router(admin, tags=["Admin"])
    app.include_router(publish, tags=["Publishing"])

    def _openapi():
        """The stock schema plus this deployment's public server URL (when ``base_url``
        is configured) so examples show real endpoints. No x-logo: the page's header
        bar carries the wordmark, so the sidebar starts at search."""
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version,
                             description=app.description, routes=app.routes,
                             tags=_OPENAPI_TAGS)
        if settings.base_url:
            schema["servers"] = [{"url": settings.base_url}]
        app.openapi_schema = schema
        return schema

    app.openapi = _openapi
    return app

"""An EPHEMERAL, per-run OTA update server for a HIL rig.

Each test rig spins up its OWN update server (plus the fake swd-ids registrar) for the
duration of a run, on the node's own LAN/WiFi IP, backed by a throwaway artifact store +
sqlite DB and a self-signed cert for that IP. Torn down when the run ends. So:

  * rigs are SELF-CONTAINED -- no shared bench server, no rig knowing another exists;
  * the tamper scenarios (corrupt/bad_sig/bad_key/bad_version), which need the harness
    CO-LOCATED with the artifact store, now run on EVERY board (the store is always local);
  * no OTA_SERVER / OTA_TOKEN needs to be a repo secret -- the harness owns both.

The board reaches the server at ``https://<node-ip>:<port>``; ``<node-ip>`` is the node's
address on the default route (the LAN the router also bridges the WiFi boards onto), so one
IP serves both LAN and WiFi legs. The device trusts the run's self-signed cert (copied to
/flash as the board CA by the harness prepare()).
"""

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.request

REGISTRAR_PORT = 8901
CERT_DIR = os.path.expanduser("~/.cache/hil-bench")   # cert is STABLE per node (see _ensure_cert)

# The fake swd-ids registrar: every device is "registered" (the registration GATE is tested
# by swd-ids' own suite; here we just need it to not block the OTA check-in).
_FAKE_REGISTRAR = (
    "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
    "import json\n"
    "class H(BaseHTTPRequestHandler):\n"
    "    def do_POST(self):\n"
    "        self.rfile.read(int(self.headers.get('Content-Length', 0)))\n"
    "        b = json.dumps({'registered': True, 'registrar_ref': 'bench'}).encode()\n"
    "        self.send_response(200); self.send_header('Content-Type', 'application/json')\n"
    "        self.send_header('Content-Length', str(len(b))); self.end_headers()\n"
    "        self.wfile.write(b)\n"
    "    def log_message(self, *a):\n"
    "        pass\n"
    "HTTPServer(('127.0.0.1', %d), H).serve_forever()\n" % REGISTRAR_PORT
)

# The ASGI launcher -- mirrors the bench's run_server.py, but every path/port/cert comes from
# the environment the harness sets, so nothing is hard-coded to one node.
_RUN_SERVER = (
    "import os\n"
    "from openmv_ota.server.cli import _settings, _store, _bootstrap, _seed_admin_token\n"
    "from openmv_ota.server.app import create_app\n"
    "import uvicorn\n"
    # migrate + cohort_salt (_bootstrap) AND seed the ADMIN_BOOTSTRAP_TOKEN into the fresh DB
    # (_seed_admin_token) -- exactly what `server init` does, so the harness's publish token works
    "s = _settings(); st = _store(s); _bootstrap(st, s); _seed_admin_token(st, s)\n"
    "app = create_app(s, metastore=st)\n"
    "uvicorn.run(app, host='0.0.0.0', port=int(os.environ['PORT']),\n"
    "            ssl_certfile=os.environ['SRV_CERT'], ssl_keyfile=os.environ['SRV_KEY'],\n"
    "            log_level='warning')\n"
)


def node_ip():
    """The node's IP on its default route -- the LAN address the router also bridges the WiFi
    boards onto, so it's reachable from both LAN and WiFi legs. No packets are sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _ensure_cert(ip):
    """A self-signed cert for the node's IP -- STABLE across runs (regenerated only if absent
    or the node's IP changed) so a board provisioned once (--skip-provision) keeps trusting it,
    and so the store, not the trust anchor, is what's fresh per run."""
    os.makedirs(CERT_DIR, exist_ok=True)
    cert, key, tag = (os.path.join(CERT_DIR, "srv.pem"), os.path.join(CERT_DIR, "srv.key"),
                      os.path.join(CERT_DIR, "ip"))
    have = (os.path.exists(cert) and os.path.exists(key) and os.path.exists(tag)
            and open(tag).read().strip() == ip)
    if not have:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", key, "-out", cert, "-days", "3650",
             "-subj", "/CN=%s" % ip, "-addext", "subjectAltName=IP:%s" % ip],
            check=True, capture_output=True)
        open(tag, "w").write(ip)
    return cert, key


def _peer_cert_der(url, timeout=3):
    """The DER certificate the server at ``url`` actually presents (no verification)."""
    hostport = url.split("://", 1)[1]
    host, _, port = hostport.partition(":")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, int(port or 443)), timeout=timeout) as sock, \
            ctx.wrap_socket(sock, server_hostname=host) as tls:
        return tls.getpeercert(binary_form=True)


def _wait_ready(url, ca, timeout):
    ctx = ssl.create_default_context(cafile=ca)
    ctx.check_hostname = False                       # readiness poll only -- the DEVICE verifies
    ctx.verify_mode = ssl.CERT_NONE
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/healthz", context=ctx, timeout=3) as r:
                if r.status == 200:
                    _assert_is_our_server(url, ca)
                    return
        except Exception as e:                       # not up yet
            last = e
        time.sleep(1)
    raise RuntimeError("ephemeral OTA server never became ready at %s (%s)" % (url, last))


def _assert_is_our_server(url, ca):
    """Fail LOUDLY if something *else* is answering on our port.

    The readiness poll deliberately does not verify (the DEVICE is what verifies), so a
    server left behind by an earlier run answers it perfectly happily -- and then the whole
    run publishes into, and tampers with, a store that is not the one it thinks it owns.
    That is a gate that LIES: it reported `certificate verify failed: self-signed certificate`
    from `client publish` and read as a device/code failure, on a PR whose device code was fine.

    The port-freeing above cannot prevent it: `pkill`/`fuser` run as whoever launched the run,
    and the orphan belonged to another user (a bench run started by hand, killed without its
    child), so both silently no-op'd -- they are `check=False` with output captured. The cert
    is per-user, so the stale server presents a different one, which is exactly what makes this
    detectable: compare what the port PRESENTS against the cert we just handed our own server.
    """
    try:
        theirs = _peer_cert_der(url)
    except Exception:
        return                                       # transient; the poll above owns liveness
    with open(ca) as fh:
        ours = ssl.PEM_cert_to_DER_cert(fh.read())
    if theirs != ours:
        raise RuntimeError(
            "%s is served by a DIFFERENT process than the one this run started -- it presents a "
            "certificate we did not issue. Almost certainly an orphaned bench server from an "
            "earlier run (often another user's, which is why pkill/fuser above could not clear "
            "it). Clear it and re-run:\n"
            "    pkill -9 -f 'openmv_ota.server.cli'; fuser -k %s/tcp"
            % (url, url.rsplit(":", 1)[1]))


def start(python, port=8443, token="bench-admin-token-1", log=print, offer_downgrades=False):
    """Bring up the per-run server + registrar. Returns a handle for ``stop()``; also carries
    ``url`` / ``ca`` / ``store`` / ``token`` for the harness to point CFG at."""
    # Free the port first: a lingering server (a prior crashed run, or the old shared one) would
    # otherwise shadow this run's store -> tamper/publish would hit the wrong artifacts.
    subprocess.run(["pkill", "-f", "run_server\\|_RUN_SERVER\\|uvicorn"], check=False,
                   capture_output=True)
    subprocess.run(["fuser", "-k", "%d/tcp" % port], check=False, capture_output=True)
    time.sleep(1)
    ip = node_ip()
    cert, key = _ensure_cert(ip)                      # stable per node
    d = tempfile.mkdtemp(prefix="hil-otasrv-")        # store + DB: FRESH per run
    store = os.path.join(d, "artifacts")
    os.makedirs(store, exist_ok=True)
    url = "https://%s:%d" % (ip, port)
    env = dict(
        os.environ,
        OPENMV_OTA_BASE_URL=url,
        OPENMV_OTA_DATABASE_URL="sqlite:///%s/ota.db" % d,
        OPENMV_OTA_STORAGE_BACKEND="local",
        OPENMV_OTA_STORAGE_LOCATION=store,
        OPENMV_OTA_SWD_IDS_VERIFY_URL="http://127.0.0.1:%d/verify" % REGISTRAR_PORT,
        OPENMV_OTA_SWD_IDS_VERIFY_TOKEN="benchtoken",
        OPENMV_OTA_ADMIN_BOOTSTRAP_TOKEN=token,
        OPENMV_OTA_POLL_AFTER_S="5",
        # OFF unless the scenario needs it. It relaxes the server's anti-rollback OFFER gate, which
        # only `bad_version` wants (it exists to feed the DEVICE an offer a correct server would
        # never make, so the device's own rejection can be tested). Left on for EVERY run it makes
        # the server re-offer a release the device has already installed -- so a device that keeps
        # running past its promotion is told to install the same version again, forever. That is
        # exactly what happened once a board finally ran past its promotion: install -> confirm ->
        # re-offer -> re-install -> `image sha256 does not match the manifest` -> fall back to
        # golden -> re-offer, on a loop, which reads as an OTA fault and is a bench misconfiguration.
        **({"OPENMV_OTA_TEST_OFFER_DOWNGRADES": "1"} if offer_downgrades else {}),
        PORT=str(port), SRV_CERT=cert, SRV_KEY=key)
    slog = open(os.path.join(d, "server.log"), "w")
    reg = subprocess.Popen([python, "-c", _FAKE_REGISTRAR],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    srv = subprocess.Popen([python, "-c", _RUN_SERVER], env=env, stdout=slog, stderr=slog)
    log("bench-server: %s  (store %s)" % (url, store))
    try:
        _wait_ready(url, cert, timeout=60)
    except Exception:
        stop({"procs": [srv, reg], "dir": d})
        raise
    return {"url": url, "ca": cert, "store": store, "token": token, "ip": ip,
            "dir": d, "procs": [srv, reg]}


def stop(handle):
    if not handle:
        return
    for p in handle.get("procs", []):
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    d = handle.get("dir")
    if d and os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)

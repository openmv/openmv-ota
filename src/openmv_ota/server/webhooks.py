"""Webhooks: the audit log's push side.

Every audit entry is an event. An account subscribes an HTTPS endpoint to some of them;
the store queues one delivery per matching entry as the entry is written, and this
module sends them: a JSON body signed with the endpoint's secret, retried on a fixed
backoff, dead after the last attempt, and the endpoint disabled after too many dead
deliveries in a row. At-least-once, in audit order per endpoint as far as retries allow;
a consumer dedupes on the event id and can always reconcile from `GET /audit?since=`.

What is deliberately not here: response bodies (only the status code is kept), any
secret or token in a payload, and delivery to private addresses unless a self-host
says so -- an endpoint URL is customer input, and a server that will POST to anything
it is told to is a proxy into its own network.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import time
from urllib.parse import urlsplit

# The event catalogue: every audit action a subscriber may name, and what it means.
# Actions not listed here still flow (a subscription to "*" or a family gets them) --
# this is the documented, stable set.
EVENTS = {
    "device.enrolled": "a camera checked in for the first time under the account",
    "device.refused": "a new camera was turned away at the account's device limit",
    "device.fallback": "a camera booted its previous image after a failed update",
    "device.bind": "a camera was bound to the account by an operator",
    "device.forget": "a camera was removed from the fleet (its data with it)",
    "device.rename": "a camera's display name changed",
    "device.pin": "a camera was pinned to a release, or unpinned",
    "install.failed": "a camera reported an install that failed",
    "release.publish": "a release was published",
    "release.rename": "a release's display name changed",
    "rollout.create": "a rollout started",
    "rollout.update": "a rollout was raised, paused, resumed or its failure limit changed",
    "rollout.autopause": "a rollout paused itself at its failure limit",
    "rollout.stop": "a rollout was stopped for good",
    "rollout.superseded": "a rollout was replaced by a newer one for the same cohort",
    "rollout.rename": "a rollout's display name changed",
    "cohort.create": "a cohort was declared",
    "cohort.assign": "devices moved into a cohort",
    "cohort.pin": "a cohort was pinned to a release, or unpinned",
    "cohort.rename": "a cohort was renamed",
    "cohort.delete": "a cohort was deleted",
    "product.create": "a product was declared",
    "product.rename": "a product's display name changed",
    "advisory.found": "a security advisory was found for a release the fleet runs",
    "account.limit": "the account's device limit changed",
    "account.rename": "the account was renamed",
    "account.activate": "the account was re-enabled",
    "account.deactivate": "the account was deactivated",
    "token.revoke": "an API token was revoked",
    "webhook.create": "a webhook endpoint was added",
    "webhook.update": "a webhook endpoint was changed",
    "webhook.delete": "a webhook endpoint was removed",
    "webhook.disabled": "a webhook endpoint was disabled after failing too many times in a row",
    "webhook.ping": "a test event, sent on request",
}

# Attempt n waits BACKOFF[n-1] before the next; after the last, the delivery is dead.
BACKOFF_S = (60, 300, 1800, 7200, 43200, 86400)
MAX_ATTEMPTS = len(BACKOFF_S) + 1
DISABLE_AFTER = 50                 # consecutive failed attempts before the endpoint is switched off
MAX_ENDPOINTS = 20                 # per account


class WebhookError(Exception):
    """A URL the server will not deliver to; the message says why."""


def sign(secret: str, ts: int, body: bytes) -> str:
    """``t=<ts>,v1=<hex>``: HMAC-SHA256 over ``<ts>.<body>``, the Stripe shape, so a receiver
    can reject replays older than it likes."""
    mac = hmac.new(secret.encode(), b"%d." % ts + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def check_url(url: str, *, allow_private: bool = False, resolve=None) -> str:
    """The endpoint URL, or a WebhookError. HTTPS only (HTTP is allowed to loopback for a
    self-host that opted in); the host must resolve, and to no private, loopback,
    link-local or multicast address unless private delivery is allowed."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise WebhookError("the URL must be https://host/path")
    if parts.scheme == "http" and not allow_private:
        raise WebhookError("the URL must use https")
    if parts.username or parts.password:
        raise WebhookError("credentials in the URL are not accepted")
    if len(url) > 2048:
        raise WebhookError("the URL is too long")
    resolve = resolve or socket.getaddrinfo      # looked up at call time, so tests can stub it
    try:
        infos = resolve(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError:
        raise WebhookError("the host does not resolve") from None
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not allow_private and (addr.is_private or addr.is_loopback or addr.is_link_local
                                  or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
            raise WebhookError("the host resolves to a private address")
    return url.strip()


def payload_for(hook: dict, entry: dict, delivery: dict) -> dict:
    """What a receiver gets: the audit entry, as the log holds it, plus how it got there."""
    return {
        "id": f"evt_{entry['seq']}",
        "type": entry["action"],
        "account_id": entry.get("account_id") or "",
        "time": entry["ts"],
        "actor": entry.get("actor") or "",
        "entity": {"type": entry.get("entity_type"), "id": entry.get("entity_id")},
        "product_id": entry.get("product_id"),
        "data": entry.get("data") or {},
        "seq": entry["seq"],
        "delivery": {"id": delivery["delivery_id"], "attempt": delivery["attempt"] + 1,
                     "webhook_id": hook["webhook_id"]},
    }


class Deliverer:
    """Sends what the store has queued. ``run_once`` is what the worker thread and the
    tests call; ``http`` is any object with ``post(url, content=, headers=, timeout=)``."""

    def __init__(self, metastore, settings, http=None):
        self._ms = metastore
        self._settings = settings
        self._http = http

    def _client(self):
        if self._http is None:                     # pragma: no cover - wired in prod only
            import httpx
            self._http = httpx.Client(timeout=self._settings.webhook_timeout_s,
                                      follow_redirects=False)
        return self._http

    def run_once(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        sent = failed = dead = 0
        for d in self._ms.claim_due_deliveries(now):
            hook = self._ms.get_webhook(d["webhook_id"])
            if hook is None or not hook["active"]:
                self._ms.finish_delivery(d["delivery_id"], status="dead", attempt=d["attempt"],
                                         code=None, error="endpoint gone or disabled", next_at=now)
                dead += 1
                continue
            entry = self._ms.get_audit_entry(d["audit_seq"])
            if entry is None:                      # pragma: no cover - the log is append-only
                self._ms.finish_delivery(d["delivery_id"], status="dead", attempt=d["attempt"],
                                         code=None, error="audit entry missing", next_at=now)
                dead += 1
                continue
            body = json.dumps(payload_for(hook, entry, d), separators=(",", ":"), sort_keys=True).encode()
            ts = int(now)
            headers = {"Content-Type": "application/json",
                       "User-Agent": "openmv-ota-webhooks/1",
                       "X-OpenMV-Event": entry["action"],
                       "X-OpenMV-Event-Id": f"evt_{entry['seq']}",
                       "X-OpenMV-Delivery": d["delivery_id"],
                       "X-OpenMV-Attempt": str(d["attempt"] + 1),
                       "X-OpenMV-Signature": sign(hook["secret"], ts, body)}
            code, error = None, ""
            try:
                r = self._client().post(hook["url"], content=body, headers=headers,
                                        timeout=self._settings.webhook_timeout_s)
                code = r.status_code
                if not 200 <= code < 300:
                    error = f"HTTP {code}"
            except Exception as e:                 # noqa: BLE001 - any transport failure is a retry
                error = str(e)[:200] or type(e).__name__
            attempt = d["attempt"] + 1
            if not error:
                self._ms.finish_delivery(d["delivery_id"], status="delivered", attempt=attempt,
                                         code=code, error="", next_at=now)
                self._ms.note_webhook_result(hook["webhook_id"], ok=True, code=code)
                sent += 1
                continue
            failures = self._ms.note_webhook_result(hook["webhook_id"], ok=False, code=code)
            if attempt >= MAX_ATTEMPTS:
                self._ms.finish_delivery(d["delivery_id"], status="dead", attempt=attempt,
                                         code=code, error=error, next_at=now)
                dead += 1
            else:
                self._ms.finish_delivery(d["delivery_id"], status="pending", attempt=attempt,
                                         code=code, error=error, next_at=now + BACKOFF_S[attempt - 1])
                failed += 1
            if failures >= DISABLE_AFTER:
                self._ms.disable_webhook(hook["webhook_id"],
                                         f"{failures} consecutive failed deliveries")
                self._ms.append_audit(actor="webhooks", action="webhook.disabled",
                                      entity_type="webhook", entity_id=hook["webhook_id"],
                                      data={"failures": failures, "last_error": error},
                                      account_id=hook["account_id"])
        return {"sent": sent, "failed": failed, "dead": dead}

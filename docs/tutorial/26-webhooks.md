# 26 · Webhooks

Everything the server does lands in the audit log, in order, and
[Integrating as a platform](25-platform-integration.md) shows how to read that log
forward with `GET /audit?since=`. Webhooks are the same log, pushed: an HTTPS endpoint
of yours subscribes to some of the account's events, and every matching entry is
POSTed to it as it is written. Polling stays available, and is the reconcile path
if a delivery is ever missed.

## Subscribing an endpoint

```
$ openmv-ota client webhook create --url https://ops.example.com/openmv --events rollout.autopause,device.fallback,install.failed,advisory.found --description "fleet health pager"
webhook wh_5a1e9c0d2b7f4e31 created for https://ops.example.com/openmv
signing secret (store it now -- not recoverable): whsec_Q3nZk9...
```

`--events` is a comma-separated list of event types, families (`rollout.*`) or `*`
for everything. The secret is shown once; `webhook rotate` mints a new one and the old
one stops verifying at that moment. An account may hold twenty endpoints. The URL
must be `https://` and resolve to a public address — the server will not POST into
private networks (a self-host can allow it with `OPENMV_OTA_WEBHOOK_ALLOW_PRIVATE=1`).

`webhook list` shows every endpoint and the event catalogue; `webhook update` changes
the URL, the subscription or the description, or switches the endpoint off and on;
`webhook delete` removes it with its delivery history.

## The events

| Event | Fires when |
|---|---|
| `device.enrolled` | a camera checks in for the first time under the account |
| `device.refused` | a new camera is turned away at the account's device limit |
| `device.fallback` | a camera boots its previous image after a failed update |
| `install.failed` | a camera reports an install that failed |
| `device.bind`, `device.forget`, `device.rename`, `device.pin` | operator actions on a camera |
| `release.publish`, `release.rename` | a release is published, or relabelled |
| `rollout.create`, `rollout.update`, `rollout.stop`, `rollout.superseded`, `rollout.rename` | a rollout's lifecycle |
| `rollout.autopause` | a rollout paused itself at its failure limit |
| `cohort.create`, `cohort.assign`, `cohort.pin`, `cohort.rename`, `cohort.delete` | cohort changes |
| `product.create`, `product.rename` | product changes |
| `advisory.found` | a scan found a **new** security advisory for a release the fleet runs (one event per finding, never one per scan) |
| `account.limit`, `account.rename`, `account.activate`, `account.deactivate` | operator actions on the account |
| `token.revoke` | an API token was revoked |
| `webhook.create`, `webhook.update`, `webhook.delete`, `webhook.disabled` | changes to endpoints, including the server switching one off |
| `webhook.ping` | the test event `webhook test` sends |

Check-ins themselves are never events: that would be one per camera per interval. What
a check-in changes (the first one, a fallback, a refusal) is.

## What arrives

One JSON body per event — the audit entry exactly as the log holds it, plus how it got
to you:

```json
{
  "id": "evt_4821",
  "type": "rollout.autopause",
  "account_id": "acct_7bd21c50e83a94f1",
  "time": "2026-09-21T18:46:52.331Z",
  "actor": "checkin",
  "entity": {"type": "rollout", "id": "ro_1a618704bcf61837"},
  "product_id": 5553380507785669254,
  "data": {"attempted": 7, "failures": 1, "threshold": 0.05},
  "seq": 4821,
  "delivery": {"id": "dl_9f0c2e11a4b37d58", "attempt": 1, "webhook_id": "wh_5a1e9c0d2b7f4e31"}
}
```

`id` and `seq` are the audit entry's sequence number: dedupe on `id`, and if you ever
need to be sure you have everything, `GET /audit?since=<seq>` from the last one you saw.

The headers carry the same identity, and the signature:

```
Content-Type: application/json
X-OpenMV-Event: rollout.autopause
X-OpenMV-Event-Id: evt_4821
X-OpenMV-Delivery: dl_9f0c2e11a4b37d58
X-OpenMV-Attempt: 1
X-OpenMV-Signature: t=1758480412,v1=5b8c…
```

## Verifying the signature

`v1` is HMAC-SHA256, under the endpoint's secret, of the timestamp, a dot, and the raw
body. Compute it over the bytes you received, not a re-serialisation:

```python
import hashlib, hmac, time

def verify(secret: str, header: str, body: bytes, tolerance_s: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    if abs(time.time() - int(parts["t"])) > tolerance_s:
        return False                                  # a replay of an old delivery
    expect = hmac.new(secret.encode(), parts["t"].encode() + b"." + body,
                      hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, parts["v1"])
```

Answer any 2xx once you have stored the event. Do the work afterwards: a receiver that
takes longer than ten seconds is a failed delivery, and will be sent again.

## Retries, and when the server gives up

A delivery that does not get a 2xx within ten seconds is retried after 1 minute, then
5, 30, 2 hours, 12 and 24 — seven attempts over about a day and a half — then marked
**dead**. Delivery is at least once: a retry after a timeout may repeat an event your
receiver did in fact store, which is what `id` is for.

An endpoint that fails fifty attempts in a row is switched off, its pending deliveries
die, and the account gets a `webhook.disabled` event (to its other endpoints) and an
audit entry. Fix the receiver, then `webhook update --enable`; the failure count
starts from zero.

```
$ openmv-ota client webhook deliveries --webhook-id wh_5a1e9c0d2b7f4e31 --status dead
{
  "deliveries": [
    { "delivery_id": "dl_9f0c2e11a4b37d58", "event": "rollout.autopause", "status": "dead",
      "attempt": 7, "last_code": 503, "last_error": "HTTP 503", "audit_seq": 4821, ... }
  ],
  "total": 1
}

$ openmv-ota client webhook retry --webhook-id wh_5a1e9c0d2b7f4e31 --delivery-id dl_9f0c2e11a4b37d58
delivery dl_9f0c2e11a4b37d58 queued again
```

`webhook test` queues a `webhook.ping` to one endpoint regardless of its subscription,
so a new receiver can be checked end to end before anything real happens.

## Running it yourself

The worker that sends deliveries lives in the server process; set
`OPENMV_OTA_WEBHOOK_INTERVAL_S` (OpenMV's deployment uses 15) or nothing is sent.
Deliveries are leased per attempt, so several server processes never send the same
one twice. See [Self-hosting](21-self-hosting.md).

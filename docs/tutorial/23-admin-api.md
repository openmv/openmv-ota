# The admin API

*[← 22 · The device API](22-device-api.md) · [Index](00-introduction.md)*

---

Everything the `client` verb does is a call to this API — so anything the CLI can do,
your scripts, CI, and dashboards can do too.

Requests carry `Authorization: Bearer <token>`. A token belongs to an **account** and
carries **scopes** (`publish`, `manage`, `observe`, `accounts`); every read and write
is scoped to the token's account, and anything belonging to another account answers
**404** — indistinguishable from "doesn't exist", so the API can't be used to probe
other tenants.

## The reference is the server itself

The endpoint-by-endpoint reference deliberately does not live in this tutorial, where
it would drift: it is generated from the running code and served by every deployment.
Open **`/docs`** on your server for the browsable version — every operation, grouped
and searchable, with its request and response schemas — or fetch **`/openapi.json`**
for the machine-readable contract, and generate a client from it instead of
hand-writing one against guesses. The CLI pages ([15](15-the-client.md) onward) walk
the same surface in workflow order; `/docs` is wire order, and always exactly what
your server speaks.

## Paging

The collection reads (`/releases`, `/rollouts`, `/devices`) take `limit` (default 100)
and `offset`, and every response carries **`total`** beside its rows:

```json
{ "releases": [ ... ], "total": 412 }
```

`total` is what makes a page safe to consume — without it, a full page is
indistinguishable from a complete list. It is account-scoped like the rows it counts, so
it never discloses the size of another tenant's fleet.

`/audit` pages differently on purpose: it is an append-only log, so it takes `since`
(each event's sequence number is a cursor) rather than an offset — a cursor can't skip or
repeat entries when new ones arrive mid-page, which an offset can.

## The OpenAPI contract

Every operation documents its response schema, so `/docs` shows real shapes and
`/openapi.json` can drive a generated client. The schemas are attached as
**documentation, not enforcement**: responses are never filtered through them, so a field
the schema lags behind on still reaches the caller — the cost of a stale schema is an
incomplete doc, never lost data.

## See also

- [15 · The client](15-the-client.md) and the pages after it — the CLI over this API.

---

*[← 22 · The device API](22-device-api.md) · [Index](00-introduction.md)*

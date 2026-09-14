# Pulling device data

*[← 23 · The admin API](23-admin-api.md) · [Index](00-introduction.md)*

---

Everything a camera sends besides check-ins — its console lines, its telemetry —
lands in the **datalake**, a separate service with its own API. Your admin token never
opens it directly. Instead the update server mints a short-lived **viewer grant** for one
device, and that grant's own token opens the datalake's read endpoints. The CLI hides the
hand-off; the raw calls are below for scripts in other languages.


## With the CLI

`client data topics` is the place to start: what the device has been sending, with every
field's type (from the JSON the device wrote) and its latest value:

```
$ openmv-ota client data topics --device-id cam-0f3a
{
  "topics": [
    { "topic": "console", "objects": 812, "bytes": 96410, "records": 3120, "seq_max": 3120,
      "fields": {}, "latest": {}, "latest_ts": null },
    { "topic": "telemetry", "objects": 640, "bytes": 402113, "records": 6400, "seq_max": 6400,
      "fields": { "temp_c": "number", "fps": "number", "detections": "number", "ok": "bool" },
      "latest": { "temp_c": 34.5, "fps": 27.1, "detections": 3, "ok": true },
      "latest_ts": 1789339520.0 }
  ]
}
```

A topic with no `fields` is text: read it as a log. `client data logs` pages the newest
boot session, newest records last, and `next_before_seq` is the cursor for the page
before it:

```
$ openmv-ota client data logs --device-id cam-0f3a --topic console --limit 3
{
  "sid": "3f9a1c",
  "records": [
    { "sid": "3f9a1c", "seq": 3118, "ts": 1789339501.2, "line": "wifi: rssi -58" },
    { "sid": "3f9a1c", "seq": 3119, "ts": 1789339511.2, "line": "app: detections=3" },
    { "sid": "3f9a1c", "seq": 3120, "ts": 1789339520.0, "line": "app: frame pipeline ok" }
  ],
  "next_before_seq": 3118
}
$ openmv-ota client data logs --device-id cam-0f3a --topic console --before-seq 3118 --limit 3
```

A numeric field comes back downsampled, so a week of samples is at most `--buckets`
points, each with the bucket's `min`, `avg`, `max` and sample count `n`. The window
defaults to the topic's whole span; `--since` / `--until` are epoch seconds:

```
$ openmv-ota client data series --device-id cam-0f3a --topic telemetry --field temp_c \
      --since $(date -d '-1 day' +%s) --until $(date +%s) --buckets 4
{
  "field": "temp_c", "since": 1789253120.0, "until": 1789339520.0, "truncated": false,
  "buckets": [
    { "t": 1789253120.0, "n": 96, "min": 31.2, "max": 40.1, "avg": 35.8 },
    ...
  ]
}
```

Nested fields address by dotted path (`--field imu.ax`). `truncated: true` means the scan
hit the server's object cap for one request: narrow the window rather than trust a
partial chart.


## Without the CLI

Two calls. First the grant, with any admin token that has `observe` — it is scoped to
one device, and a device you don't own is a 404 like everything else:

```
$ curl -s -X POST -H "Authorization: Bearer $OPENMV_OTA_TOKEN" \
      https://ota.cloud.openmv.io/api/v1/admin/devices/cam-0f3a/viewer-grant
{
  "token": "...",                       # the live relay's watch token
  "streams": { ... },
  "expires_in_s": 300,
  "datalake": {
    "token": "...",                     # THIS one opens the datalake
    "topics_url": "https://data.cloud.openmv.io/api/v1/topics/cam-0f3a",
    "logs_url":   "https://data.cloud.openmv.io/api/v1/logs/cam-0f3a",
    "series_url": "https://data.cloud.openmv.io/api/v1/series/cam-0f3a",
    "expires_in_s": 300
  }
}
```

Then the read, under the datalake token, at the URL the grant named (`logs_url` and
`series_url` take `/{topic}` on the end):

```
$ curl -s -H "Authorization: Bearer $LAKE_TOKEN" \
      "https://data.cloud.openmv.io/api/v1/series/cam-0f3a/telemetry?field=fps&buckets=50"
```

Grants expire in minutes by design: mint one per run, not one per month. `client device
grant --device-id ...` prints exactly this grant if you want the CLI to do only that half.
The datalake's own endpoint reference is at `https://data.cloud.openmv.io/docs`.


---

*[← 23 · The admin API](23-admin-api.md) · [Index](00-introduction.md)*

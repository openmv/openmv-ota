# Pulling device data

*[← 23 · The admin API](23-admin-api.md) · [Index](00-introduction.md) · [25 · Integrating as a platform →](25-platform-integration.md)*

---

Everything a camera sends besides check-ins — its console lines, its telemetry —
lands in the **datalake**, a separate service with its own API. Your admin token never
opens it directly. Instead the update server mints a short-lived **viewer grant** for one
device, and that grant's own token opens the datalake's read endpoints. The CLI hides the
hand-off; the raw calls are below for scripts in other languages.


## Sending data from the camera

Nothing about a metric is declared anywhere: not in the project, not on the server. The
camera posts JSON, and the datalake works out from the JSON itself what each value is and
how to show it. The generated `main.py`'s heap graph is the whole pattern:

```python
import asyncio
import gc

from openmv_cloud import datalog

datalog.enable()                      # once, at start-up

async def heap_graph():
    while True:
        used, free = gc.mem_alloc(), gc.mem_free()
        datalog.post("heap", {"used_pct": round(100 * used / (used + free), 1)})
        await asyncio.sleep(5)
```

The percentage is computed on the camera, by that one line. The server knows nothing
about heaps: it sees a topic called `heap` carrying a number called `used_pct`, and the
device page shows it as a tile labelled **Used %** and a line on the **Heap** chart.

### Posting

`datalog.post(topic, obj)` queues one record and returns at once; a background task
uploads the queue in batches every 5 seconds. It returns `True` when the record is
queued and `False` when it is dropped:

- the topic name is not 1–32 characters of lowercase letters, digits, `_` and `-`
  (starting with a letter or digit), or it is `console`, which the console log uses;
- the camera already has 32 topics;
- the board's level is `ota-only`, which has no datalake.

Each record is stored with a boot-session id and a sequence number, so a batch the
camera re-sends after a dropped connection is stored once, and with the time it was
taken whenever the camera's clock has been set. Your object is kept as you posted it.

On the camera, records wait in RAM, all topics sharing one byte budget. Many topics,
each posting slowly, is the intended use. `openmv_cloud.configure()` sets the budget.

Uploading reuses one connection and fixed buffers, so it adds next to nothing to the heap.
What a camera app cannot avoid allocating -- the camera returns a new image object every
frame -- is collected by the SDK after a little has piled up (2% of the heap by default),
which keeps the heap chart flat instead of climbing to full and dropping back. Change the
amount with `openmv_cloud.configure(gc_bytes=...)`.

### What becomes a field

Every value in the posted object becomes a **field**, typed by its JSON:

| JSON value | Field | On the device page |
|------------|-------|--------------------|
| a number | number | a tile with the latest value, and a line you can chart |
| `true` / `false` | bool | an On / Off tile |
| a string | string | a tile with the latest text (not charted) |
| an object | its members, by dotted name | `{"imu": {"ax": 0.1}}` is the field `imu.ax` |
| a list, or `null` | — | not shown |

A field's type is whatever the topic's newest record says, so changing what you post
changes the page with it. A record charts at most 32 numbers, and a dotted name longer
than 128 characters is not charted.

### On the device page

The **Data** section draws one block per topic that has at least one number field:

- **Tiles**: one per field with its latest value. The **Tiles** menu picks which (the
  first 12, until you choose).
- **Chart**: up to two number fields at once, each on its own axis, picked from the
  **Series** menu. When a point stands for several samples, a lone field also shows
  their min and max as dashed lines.
- **Range**: the last 15 minutes up to the last month, or a custom window. While the
  window reaches the present, the chart and tiles update by themselves: every 5 seconds
  for a window up to an hour, less often for longer ones. A hidden browser tab doesn't
  update and catches up when shown.
- **Topics**: the first three topics are shown until you pick others from the
  **Topics** menu.
- **CSV**: the chart's fields over its window, one row per point.

Samples are kept summarized into 5-second and 60-second slots for charting, so a chart
point never covers less than 5 seconds. A product's page draws the same section across
all of its devices: each number is the average of the devices' latest values, with the
lowest and highest beside it.

Labels come from the names. A name splits on `_` and `.`, its first word is
capitalized, and these words become units:

| In the name | Shown as |
|-------------|----------|
| `pct`, `percent` | % |
| `c` | °C |
| `f` | °F |
| `ms`, `s` | ms, s |
| `mv`, `v`, `ma` | mV, V, mA |
| `kb`, `mb` | KB, MB |
| `fps`, `rssi`, `cpu`, `ram`, `ip`, `imu`, `id`, `ok` | FPS, RSSI, CPU, RAM, IP, IMU, ID, OK |

So `used_pct` reads **Used %**, `temp_c` **Temp °C** and `imu.ax` **IMU ax**. The name you
posted stays the field's name everywhere else: the CSV, the API and the CLI.

### Adding a metric

Post it. Fields that belong on one chart, and are measured at the same moment, go in one
topic; anything with its own rhythm gets its own topic and its own block on the page:

```python
datalog.post("telemetry", {"temp_c": temp_c, "fps": clock.fps(),
                           "detections": len(blobs), "ok": healthy})
datalog.post("power", {"supply_mv": supply_mv, "load_ma": load_ma})
```

Put the unit at the end of the name (`_c`, `_pct`, `_ms`, `_mv`) and the label says it.
Post as often as the metric changes, not faster: every record counts against your
account's daily data budget, and the page can't show anything finer than 5 seconds
anyway. Batches over the budget are refused for the rest of the day and counted on the
dashboard.


## With the CLI

`client data topics` is the place to start: what the device has been sending, with every
field's type (from the JSON the device wrote) and its latest value:

```
$ openmv-ota client data topics --device-id OPENMV_N6:30003d000851303436313832
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
$ openmv-ota client data logs --device-id OPENMV_N6:30003d000851303436313832 --topic console --limit 3
{
  "sid": "3f9a1c",
  "records": [
    { "sid": "3f9a1c", "seq": 3118, "ts": 1789339501.2, "line": "wifi: rssi -58" },
    { "sid": "3f9a1c", "seq": 3119, "ts": 1789339511.2, "line": "app: detections=3" },
    { "sid": "3f9a1c", "seq": 3120, "ts": 1789339520.0, "line": "app: frame pipeline ok" }
  ],
  "next_before_seq": 3118
}
$ openmv-ota client data logs --device-id OPENMV_N6:30003d000851303436313832 --topic console --before-seq 3118 --limit 3
```

A numeric field comes back downsampled, so a week of samples is at most `--buckets`
points, each with the bucket's `min`, `avg`, `max` and sample count `n`. The window
defaults to the topic's whole span; `--since` / `--until` are epoch seconds:

```
$ openmv-ota client data series --device-id OPENMV_N6:30003d000851303436313832 --topic telemetry --field temp_c \
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


## A whole product at once

The same two reads answer for every device of a product together, from the
product's viewer grant instead of a device's. `topics` keeps the device shape, but
`latest` is the fleet's reading — the average of each device's latest number, or how
many devices report true for a bool — and `spread` carries the min, the max and how
many devices are behind each figure:

```
$ openmv-ota client data topics --product-id 5553380507785669254
{
  "topics": [
    { "topic": "telemetry", "objects": 5120, "bytes": 3218902, "records": 51200, "devices": 412,
      "fields": { "temp_c": "number", "fps": "number", "ok": "bool" },
      "latest": { "temp_c": 34.1, "fps": 26.8, "ok": 409 },
      "spread": { "temp_c": { "min": 29.6, "max": 41.2, "n": 412 },
                  "fps": { "min": 19.0, "max": 30.1, "n": 412 }, "ok": { "n": 412, "true": 409 } },
      "latest_ts": 1789339520.0, "truncated": false }
  ]
}
```

`series --product-id` pools every device's samples into each bucket, so the band
between `min` and `max` is the fleet's spread and `devices` counts the contributors:

```
$ openmv-ota client data series --product-id 5553380507785669254 --topic telemetry --field temp_c --buckets 4
{ "field": "temp_c", "since": 1789253120.0, "until": 1789339520.0, "truncated": false, "devices": 412,
  "buckets": [ { "t": 1789253120.0, "n": 39552, "min": 28.9, "max": 42.0, "avg": 34.6 }, ... ] }
```

Strings do not aggregate, so a product's `fields` carries numbers and bools only.
`client product grant --product-id ...` prints the grant itself.


## Without the CLI

Two calls. First the grant, with any admin token that has `observe` — it is scoped to
one device, and a device you don't own is a 404 like everything else:

```
$ curl -s -X POST -H "Authorization: Bearer $OPENMV_OTA_TOKEN" \
      https://ota.cloud.openmv.io/api/v1/admin/devices/OPENMV_N6:30003d000851303436313832/viewer-grant
{
  "token": "...",                       # the live relay's watch token
  "streams": { ... },
  "expires_in_s": 300,
  "datalake": {
    "token": "...",                     # THIS one opens the datalake
    "topics_url": "https://data.cloud.openmv.io/api/v1/topics/OPENMV_N6:30003d000851303436313832",
    "logs_url":   "https://data.cloud.openmv.io/api/v1/logs/OPENMV_N6:30003d000851303436313832",
    "series_url": "https://data.cloud.openmv.io/api/v1/series/OPENMV_N6:30003d000851303436313832",
    "expires_in_s": 300
  }
}
```

A server with a datalake but no live relay answers the same way with an empty
`token` and `streams`; only a server with neither configured refuses (503).

For many devices at once, `POST .../admin/devices/viewer-grants` with
`{"device_ids": [...]}` (up to 100) answers with the same grant per id under `grants`,
and `null` for an id that is not yours.

Then the read, under the datalake token, at the URL the grant named (`logs_url` and
`series_url` take `/{topic}` on the end):

```
$ curl -s -H "Authorization: Bearer $LAKE_TOKEN" \
      "https://data.cloud.openmv.io/api/v1/series/OPENMV_N6:30003d000851303436313832/telemetry?field=fps&buckets=50"
```

A product's grant is `POST .../admin/products/{product_id}/viewer-grant`: its `datalake`
half names a `topics_url` and a `series_url` (`+ /{topic}`) under
`/api/v1/products/{account}/{product}/...`, opened by that grant's own token.

Grants expire in minutes by design: mint one per run, not one per month. `client device
grant --device-id ...` prints exactly this grant if you want the CLI to do only that half.
The datalake's own endpoint reference is at `https://data.cloud.openmv.io/docs`.


---

*[← 23 · The admin API](23-admin-api.md) · [Index](00-introduction.md) · [25 · Integrating as a platform →](25-platform-integration.md)*

"""ROMFS build orchestration (stub).

TODO (see concept plan, Tool 3):
  1. Bundle SDK files (from this package's romfs/sdk) into the build tree.
  2. Copy the customer's app files into the build tree.
  3. Run openmv's `romfs` tool to compose the ROMFS body.
  4. Compute SHA-256; build + sign the trailer (see compose.py / sign.py).
  5. factory mode -> full FRONT+BACK partition image.
     ota mode     -> single signed slot.
  6. Append the release to releases/transparency-log.jsonl.
"""

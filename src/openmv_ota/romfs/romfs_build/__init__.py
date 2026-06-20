"""Tool 3 — ROMFS image builder.

Two modes, same underlying logic (see concept plan, "Tool 3: ROMFS builder"):

* ``factory`` — full FRONT+BACK partition image signed by the factory key, both
  slots in valid factory-state; flashed at manufacturing time.
* ``ota`` — a single signed slot (body + trailer, no status markers); uploaded
  to the update server.
"""

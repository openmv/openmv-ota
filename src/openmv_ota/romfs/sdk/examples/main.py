# Example customer main.py — full OTA integration is ~20 lines.
# (See concept plan, Tool 2.)
import machine, openmv_ota

wdt = machine.WDT(timeout=30_000)  # liveness — app's responsibility

# Customer's network bring-up.
import my_network
my_network.connect()

# OTA bootstrap — handles trial confirm, periodic check, install, reset.
openmv_ota.run(
    server_url="https://updates.acme.example",
    self_test=my_self_test_function,  # customer-provided callback  # noqa: F821
    wdt=wdt,
)

# Customer's actual app.
while True:
    wdt.feed()
    do_robot_things()  # noqa: F821

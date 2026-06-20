# Getting started

> Stub — written as the tools come online. For now this records the intended flow
> (see the concept plan, "Distribution and packaging").

```bash
pip install openmv-ota                                       # all tools, one install
openmv-ota init                                              # scaffold a customer repo
openmv-ota keys generate                                     # trusted_keys.json + keys
openmv-ota romfs build-firmware -c config/firmware.yaml      # Tool 1
openmv-ota romfs build --mode factory ... --version 1        # Tool 3 (factory image)
openmv-ota romfs build --mode ota     ... --version 2        # Tool 3 (OTA release)
openmv-ota romfs serve -c config/server.yaml                 # Tool 4 (local dev)
openmv-ota romfs publish releases/v2.bin --server URL        # upload OTA release
```

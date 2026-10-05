<img width="360" src="https://raw.githubusercontent.com/openmv/openmv-media/master/logos/openmv-logo/logo.png" alt="OpenMV">

# openmv-ota

Secure over-the-air updates for [OpenMV](https://openmv.io) cameras. Build your
application into a signed, encrypted image, publish it, and roll it out to your
cameras in stages. Each camera downloads, verifies, and installs the update, and
goes back to the last release that worked if the new one won't start.

```bash
pip install openmv-ota
```

Requires Python 3.11 or newer. One install gives you every tool, as a single
`openmv-ota` command.

## What you get

- **Signed, encrypted releases.** Every image is signed with your keys and
  encrypted for your cameras. A camera refuses anything unsigned, tampered with,
  or older than what it runs.
- **Safe installs.** Updates go to a spare slot and are confirmed only after the
  new version starts; a failed update falls back on its own.
- **Small updates.** After the first release, cameras download only what changed.
- **Staged rollouts.** Send a release to a percentage of your fleet or a cohort
  of cameras, then raise, pause, or roll it back.
- **Your keys, where you want them.** On disk (encrypted), in a PKCS#11 HSM, or in
  AWS KMS, Google Cloud KMS, or Azure Key Vault.
- **Hosted or self-hosted.** Use [OpenMV Cloud](https://cloud.openmv.io), which
  adds live video, a console, and data from every camera, or run the update
  server yourself with `pip install "openmv-ota[server]"`.
- **Compliance evidence.** An SBOM for every release, and templates for the EU
  Cyber Resilience Act and RED.

## Supported cameras

OpenMV AE3, N6, RT1062, H7 Plus and H7; Arduino Nicla Vision, Giga R1 and
Portenta H7.

## Quick start

With an [OpenMV Cloud](https://cloud.openmv.io) account, a camera on USB, and a
checkout of the [OpenMV firmware](https://github.com/openmv/openmv):

```bash
openmv-ota project new my-camera -f openmv -b OPENMV_AE3 --ota --install-sdk
openmv-ota build firmware my-camera -b OPENMV_AE3
openmv-ota build factory-romfs my-camera -b OPENMV_AE3
openmv-ota flash factory my-camera -b OPENMV_AE3
openmv-ota client login --server https://ota.cloud.openmv.io
openmv-ota client release publish my-camera -b OPENMV_AE3 -o my-camera/build/factory
```

**Getting started** on OpenMV Cloud walks through every step for your camera.

## Documentation

- [Tutorial](https://github.com/openmv/openmv-ota/blob/main/docs/tutorial/00-introduction.md):
  every command and the update server's API, in the order you use them.
- [EU CRA and RED alignment](https://github.com/openmv/openmv-ota/blob/main/docs/compliance/cra-red-alignment.md)
  and the [residual threats](https://github.com/openmv/openmv-ota/blob/main/docs/compliance/residual-threats.md).
- [Changelog](https://github.com/openmv/openmv-ota/blob/main/CHANGELOG.md).

## Security

Found a vulnerability? Please report it privately; see the
[security policy](https://github.com/openmv/openmv-ota/blob/main/SECURITY.md).

## License

MIT. Source and issues: [github.com/openmv/openmv-ota](https://github.com/openmv/openmv-ota).

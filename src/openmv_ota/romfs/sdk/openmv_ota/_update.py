"""On-device updater (stub).

The romfs_update logic from the concept plan ("Application-side updater"):
erase FRONT, stream the body computing SHA on the fly, read-back verify, VfsRom
parse check, write trailer (body commit point), write pending marker (lifecycle
commit point), then machine.reset(). Plus confirm() after self-test, and the
anti-rollback floor = max(back.image_version, front.image_version_if_valid).

Per-board notes: RT1062 blockdev fallback (rom_ioctl(3,...) == -EINVAL), and
AE3-MRAM's no-op erase needing explicit 0xFF status stripes.

TODO: implement update(), confirm(), and the per-board write paths.
"""

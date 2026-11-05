"""``openmv_ota.se`` -- the camera's keys. The code is frozen into the firmware as ``openmv_se``
(recovery needs it with no romfs); this is the name applications use. See ``openmv_se``.

RAM BUDGET: this module runs inside your application, so its memory is your memory. It allocates
nothing itself; ``openmv_se`` states its own budget."""

try:
    from openmv_se import open, provision, seal, unseal  # noqa: F401
except ImportError:                        # a board with no keys freezes no openmv_se
    def open():
        """None: this board has no keys."""
        return None

    def provision():
        """None: this board has no keys."""
        return None

    def seal(data, aad=b""):
        """Raises: this board has no keys to seal with."""
        raise ValueError("this camera has no keys to seal with")

    def unseal(blob, aad=b""):
        """Raises: this board has no keys to open a sealed blob with."""
        raise ValueError("this camera has no keys to seal with")

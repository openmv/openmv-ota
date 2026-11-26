"""``python -m openmv_ota``. ``-m`` puts the working directory first on the import path, and a
project someone else made may hold a module with a standard-library or SDK name (``argparse.py``,
``mpy_cross.py``): the directory is dropped here, before the CLI imports anything. Python has
already looked there for this package by then, so an ``openmv_ota/`` in that directory runs in
place of this one; the ``openmv-ota`` console script and ``python -P -m openmv_ota`` never look
there."""

import os
import sys

if sys.path and sys.path[0] in ("", os.getcwd()):
    del sys.path[0]

from openmv_ota.cli import main  # noqa: E402

sys.exit(main())

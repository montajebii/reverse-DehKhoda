"""PyInstaller runtime hook — preload bundled milvus_lite native libraries.

Runs BEFORE any application code inside the frozen sidecar. milvus_lite
dlopens its engine (libmilvus / libknowhere …) relative to its package
directory; if any transitive system library (libgomp, libgfortran,
libopenblas) is missing on the user's distro, the import fails with a bare
OSError. Preloading every bundled .so with RTLD_GLOBAL makes later dlopen
calls resolve by soname without needing LD_LIBRARY_PATH (which glibc reads
once at process start and cannot be changed from Python afterwards).
"""

import ctypes
import sys
from pathlib import Path

_meipass = getattr(sys, "_MEIPASS", None)
if _meipass:
    _base = Path(_meipass)
    for _d in (_base / "milvus_lite" / "lib", _base / "milvus_lite"):
        if _d.is_dir():
            for _so in sorted(_d.glob("*.so*")):
                try:
                    ctypes.CDLL(str(_so), mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass

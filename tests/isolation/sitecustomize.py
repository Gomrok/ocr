"""Inherited by Python spawn children only when the isolated test launcher opts in."""
import os
from pathlib import Path
import sys

_installed = False


def install_guard():
    global _installed
    if _installed:
        return
    configured = os.environ.get("OCR_TEST_ROOT")
    if not configured or not Path(configured).is_absolute():
        raise RuntimeError("Explicit OCR_TEST_ROOT required; no production fallback")
    root = Path(configured).resolve(strict=True)
    def require_owned(value):
        if not isinstance(value, (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(value)).resolve()
        if path != root and root not in path.parents:
            raise PermissionError("Test attempted to write outside isolated storage")
    def audit(event, args):
        if event == "open":
            mode, flags = args[1], args[2]
            if (mode and any(char in mode for char in "wax+")) or (flags and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC)):
                require_owned(args[0])
        elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime"):
            require_owned(args[0])
        elif event in ("os.rename", "os.link", "os.symlink"):
            require_owned(args[0]); require_owned(args[1])
        elif event == "sqlite3.connect":
            # Job metadata is a local SQLite file. SQLite allocates its own
            # journals, so check the database path before it can open either.
            require_owned(args[0])
    sys.addaudithook(audit)
    _installed = True


if os.environ.get("OCR_TEST_ROOT"):
    install_guard()

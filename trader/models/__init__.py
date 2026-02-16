"""Pydantic models / schemas for artifacts.

Phase 1 keeps these as simple dataclasses w/ explicit JSON serialization.
We can migrate to Pydantic later if desired.
"""

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write text to a file atomically via temp-file + rename.

    On POSIX, os.rename within the same filesystem is atomic, so a crash
    mid-write leaves either the old file or the new file, never a truncated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

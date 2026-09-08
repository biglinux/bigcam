"""Exclusive, collision-free output allocation for captures and recordings."""
from datetime import datetime
import os
from pathlib import Path
import tempfile


def reserve_media_path(directory: str, suffix: str, prefix: str = "bigcam_") -> str:
    if not suffix.startswith(".") or "/" in suffix or "/" in prefix:
        raise ValueError("Invalid media filename components")
    Path(directory).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    fd, path = tempfile.mkstemp(prefix=prefix + stamp + "_", suffix=suffix, dir=directory)
    os.close(fd)
    return path


def reserve_named_path(directory: str, filename: str) -> str:
    if not filename or Path(filename).name != filename or filename in {".", ".."}:
        raise ValueError("Filename must be a basename")
    path = Path(directory) / filename
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    return str(path)

"""Small durable JSON store shared by settings and profiles (Linux/POSIX)."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Iterator

MAX_JSON_BYTES = 1_048_576


def read_object(path: str | Path) -> dict:
    """Read an object, rejecting symlinks, oversized files and invalid roots."""
    path = os.fspath(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Configuration is not a regular file")
        raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError("Configuration exceeds the size limit")
        data = json.loads(raw, parse_constant=_invalid_number)
    if not isinstance(data, dict):
        raise ValueError("Configuration root must be an object")
    return data


def _invalid_number(value: str):
    raise ValueError(f"Non-finite JSON number: {value}")


@contextmanager
def locked(path: str | Path) -> Iterator[None]:
    """Serialize read-modify-replace across independent instances/processes.

    The lock is a separate, stable inode: replacing the data file must not replace
    the lock. Do not delete lock files while another process may be using them.
    """
    lock_path = os.fspath(path) + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Lock is not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write_object(path: str | Path, data: dict) -> None:
    """Durably replace a JSON object with private mode, without truncating it."""
    path = Path(path)
    if not isinstance(data, dict):
        raise ValueError("Configuration root must be an object")
    serialized = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if len(serialized.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("Configuration exceeds the size limit")
    if path.is_symlink():
        raise ValueError("Refusing to replace a symbolic link")
    fd, temporary = tempfile.mkstemp(prefix=".bigcam-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

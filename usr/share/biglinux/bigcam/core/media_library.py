"""Media enumeration and content-identity thumbnail caching, independent of GTK."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile

from PIL import Image, ImageOps
from utils import xdg

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mkv", ".mp4", ".webm", ".avi", ".mov"}


@dataclass(frozen=True)
class MediaEntry:
    path: str
    size: int
    modified: float
    modified_ns: int
    is_video: bool

    @property
    def name(self):
        return Path(self.path).name


def scan(directory, extensions):
    entries = []
    try:
        with os.scandir(directory) as items:
            for item in items:
                try:
                    if item.is_file(follow_symlinks=False) and Path(item.name).suffix.lower() in extensions:
                        st = item.stat(follow_symlinks=False)
                        entries.append(MediaEntry(item.path, st.st_size, st.st_mtime, st.st_mtime_ns,
                                                  Path(item.name).suffix.lower() in VIDEO_EXTS))
                except OSError:
                    continue  # A file can disappear between enumeration and stat.
    except FileNotFoundError:
        return []
    return sorted(entries, key=lambda entry: (entry.modified_ns, entry.path), reverse=True)


def thumbnail_key(entry):
    identity = f"{Path(entry.path).absolute()}\0{entry.size}\0{entry.modified_ns}"
    return hashlib.sha256(identity.encode()).hexdigest()


def thumbnail(entry):
    directory = Path(xdg.thumbs_dir())
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / (thumbnail_key(entry) + ".png")
    if target.is_file() and not target.is_symlink():
        return str(target)
    fd, temporary = tempfile.mkstemp(suffix=".png", dir=directory)
    os.close(fd)
    try:
        if entry.is_video:
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
                            "-i", entry.path, "-frames:v", "1", "-vf", "scale=160:160:force_original_aspect_ratio=decrease",
                            "-threads", "1", temporary], check=True, capture_output=True, timeout=10)
        else:
            with Image.open(entry.path) as image:
                image.draft("RGB", (320, 320))
                image = ImageOps.exif_transpose(image)
                image.thumbnail((160, 160))
                image.convert("RGB").save(temporary, format="PNG")
        if os.path.getsize(temporary) == 0:
            raise ValueError("Empty thumbnail")
        os.replace(temporary, target)
        return str(target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def duration(entry):
    if not entry.is_video:
        return ""
    result = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", entry.path],
                            check=True, capture_output=True, text=True, timeout=5)
    value = float(result.stdout)
    if not 0 <= value < 365 * 24 * 3600:
        raise ValueError("Invalid duration")
    minutes, seconds = divmod(int(value), 60)
    return f"{minutes}:{seconds:02d}"

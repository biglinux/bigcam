#!/usr/bin/python3 -I
"""Remove the exact legacy BigCam sudo rules, preserving other administrator rules."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

LEGACY = {
    f"%wheel ALL=(root) NOPASSWD: /usr/{directory}/{command}"
    for directory in ("bin", "sbin")
    for command in ("modprobe v4l2loopback *", "modprobe -r v4l2loopback",
                    "v4l2loopback-ctl add *", "v4l2loopback-ctl delete *")
}


def filtered(text: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if line.strip() not in LEGACY)


def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit("Migration requires root")
    path = Path("/etc/sudoers.d/bigcam")
    if path.is_symlink():
        raise SystemExit("Refusing symlink sudoers policy; administrator intervention required")
    if not path.exists():
        return 0
    old = path.read_text()
    new = filtered(old)
    if new == old:
        return 0
    backup_dir = Path("/var/lib/bigcam/policy-backups")
    backup_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    backup_fd, backup = tempfile.mkstemp(prefix="sudoers-", dir=backup_dir)
    with os.fdopen(backup_fd, "w") as stream:
        stream.write(old)
    fd, temporary = tempfile.mkstemp(prefix=".bigcam-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(new or "# Legacy BigCam passwordless rules removed.\n")
        os.chmod(temporary, 0o440)
        visudo = shutil.which("visudo", path="/usr/sbin:/usr/bin:/sbin:/bin")
        if visudo is None:
            raise RuntimeError("visudo is required to validate the migrated policy")
        subprocess.run([visudo, "-cf", temporary], check=True, timeout=10)
        os.replace(temporary, path)
        print(f"Removed unsafe legacy BigCam rules. Private backup: {backup}")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

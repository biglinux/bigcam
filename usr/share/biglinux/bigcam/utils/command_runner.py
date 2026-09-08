"""Subprocess boundaries with finite timeouts and credential-free diagnostics."""
from __future__ import annotations

import logging
import subprocess
from typing import Any

log = logging.getLogger(__name__)


def _arguments(args) -> list[str]:
    if not isinstance(args, (list, tuple)) or not args or any(not isinstance(a, str) or "\0" in a for a in args):
        raise ValueError("Expected a nonempty argument vector of NUL-free strings")
    return list(args)


class SecureCommandRunner:
    @staticmethod
    def run_safe(args: list[str], timeout: float = 5, capture_output: bool = True,
                 check: bool = False, **kwargs: Any) -> subprocess.CompletedProcess:
        args = _arguments(args)
        kwargs["shell"] = False
        if capture_output:
            kwargs["capture_output"] = True
        log.debug("Running %s (%d arguments)", args[0], len(args) - 1)
        return subprocess.run(args, timeout=timeout, check=check, **kwargs)

    @staticmethod
    def popen_safe(args: list[str], stdout=None, stderr=None, **kwargs: Any) -> subprocess.Popen:
        args = _arguments(args)
        kwargs["shell"] = False
        log.debug("Starting %s (%d arguments)", args[0], len(args) - 1)
        return subprocess.Popen(args, stdout=stdout, stderr=stderr, **kwargs)

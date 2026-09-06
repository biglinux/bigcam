"""Secure subprocess command runner with standardized timeouts and typing."""

import subprocess
import logging
from typing import IO, Any

log = logging.getLogger(__name__)

class SecureCommandRunner:
    """Wrapper around subprocess to ensure safety, logging, and strict typing."""

    @staticmethod
    def run_safe(
        args: list[str],
        timeout: int = 5,
        capture_output: bool = True,
        check: bool = False,
        **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        """
        Executes a subprocess securely.
        
        Args:
            args: Command and arguments as a strict list of strings.
            timeout: Maximum execution time in seconds.
            capture_output: Whether to capture stdout/stderr.
            check: Whether to raise CalledProcessError on non-zero exit status.
            **kwargs: Extra arguments passed to subprocess.run.
            
        Returns:
            subprocess.CompletedProcess
            
        Raises:
            subprocess.TimeoutExpired: If the command times out.
            FileNotFoundError: If the binary is not found.
            subprocess.CalledProcessError: If check=True and exit status is non-zero.
        """
        # Enforce shell=False for security against injection attacks
        kwargs["shell"] = False

        if capture_output:
            kwargs["capture_output"] = True

        log.debug(f"Running secure command: {' '.join(args)}")

        return subprocess.run(args, timeout=timeout, check=check, **kwargs)

    @staticmethod
    def popen_safe(
        args: list[str],
        stdout: int | IO[Any] | None = None,
        stderr: int | IO[Any] | None = None,
        **kwargs: Any
    ) -> subprocess.Popen[bytes]:
        """
        Starts a background subprocess securely.
        
        Args:
            args: Command and arguments as a strict list of strings.
            stdout: Output stream destination.
            stderr: Error stream destination.
            **kwargs: Extra arguments passed to subprocess.Popen.
            
        Returns:
            subprocess.Popen
        """
        kwargs["shell"] = False

        log.debug(f"Starting secure background process: {' '.join(args)}")

        return subprocess.Popen(args, stdout=stdout, stderr=stderr, **kwargs)

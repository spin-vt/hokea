"""Thin subprocess wrapper. Every external command goes through here so failures
are loud and carry the command line and stderr."""

import subprocess


class CommandFailed(Exception):
    def __init__(self, argv: list[str], returncode: int, stdout: str, stderr: str):
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"command failed (exit {returncode}): {' '.join(argv)}\n"
            f"stdout: {stdout.strip()}\nstderr: {stderr.strip()}"
        )


def sh(*argv: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    """Run a command, capture output. Raises CommandFailed unless check=False."""
    result = subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode != 0:
        raise CommandFailed(list(argv), result.returncode, result.stdout, result.stderr)
    return result

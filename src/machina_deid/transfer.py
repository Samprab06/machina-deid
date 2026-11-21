"""Optional storage-transfer helper: resumable rsync-over-SSH command builder.

Nothing is transferred unless ``run`` is called explicitly (CLI: ``--execute``). No
remote filesystem has to be mounted. The command never uses ``--delete`` and never
touches the source corpus: it only pushes a local, already-validated output directory.
Re-running the same command resumes (``--partial`` keeps interrupted files and rsync
skips what already matches).
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

_REMOTE = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+:(/[^\0]*)$")


class TransferError(Exception):
    pass


def build_commands(local_dir: str | Path, dest: str, *, ssh_port: int | None = None,
                   ssh_key: str | None = None, bwlimit_kbps: int | None = None) -> dict[str, list[str]]:
    """Return ``{"transfer": argv, "verify": argv}``. ``dest`` must be ``[user@]host:/abs/path``."""
    local = Path(local_dir)
    if not local.is_dir():
        raise TransferError("local directory does not exist")
    if dest.startswith("-") or not _REMOTE.match(dest):
        raise TransferError("dest must look like [user@]host:/absolute/path")
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30"]
    if ssh_port:
        ssh += ["-p", str(int(ssh_port))]
    if ssh_key:
        ssh += ["-i", ssh_key]
    base = ["rsync", "-aH", "--partial", "--partial-dir=.rsync-partial", "--info=progress2",
            "-e", shlex.join(ssh)]
    if bwlimit_kbps:
        base.append(f"--bwlimit={int(bwlimit_kbps)}")
    src = str(local.resolve()).rstrip("/") + "/"
    dst = dest.rstrip("/") + "/"
    return {"transfer": base + [src, dst],
            # checksum dry-run: lists anything that differs, changes nothing
            "verify": ["rsync", "-aHnc", "--itemize-changes", "-e", shlex.join(ssh), src, dst]}


def run(argv: list[str]) -> int:
    return subprocess.run(argv, check=False).returncode

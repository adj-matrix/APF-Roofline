"""Run external commands, or just record them (dry run)."""
from __future__ import annotations

import os
import shlex
import subprocess


def root_prefix() -> list[str]:
    """`sudo -n` unless already root (docker)."""
    return [] if os.geteuid() == 0 else ["sudo", "-n"]


class Shell:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.log: list[str] = []

    def run(self, cmd: list[str], **kw) -> str:
        self.log.append(shlex.join(cmd))
        if self.dry_run:
            return ""
        r = subprocess.run(cmd, capture_output=True, text=True, **kw)
        if r.returncode != 0:
            raise RuntimeError(f"command failed ({r.returncode}): {self.log[-1]}\n{r.stderr or r.stdout}")
        return r.stdout

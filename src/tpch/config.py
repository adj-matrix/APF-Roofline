"""Load config.env, the single source of configuration shared with the bash setup."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIG_ENV = REPO / "config.env"


@dataclass(frozen=True)
class Config:
    values: dict[str, str]
    repo: Path = REPO

    def __getattr__(self, name: str) -> str:
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(f"config.env has no {name}") from None

    def path(self, name: str) -> Path:
        return Path(self.values[name])

    def int(self, name: str) -> int:
        return int(self.values[name])

    @property
    def sfs(self) -> list[int]:
        return [int(s) for s in self.values["TPCH_SFS"].split()]

    def dbname(self, sf: int) -> str:
        return f"tpch_sf{sf}"


def load(path: Path = CONFIG_ENV, env: dict[str, str] | None = None) -> Config:
    """Evaluate config.env with bash so `${VAR:-default}` and overrides behave exactly as in setup.sh."""
    keys = re.findall(r"^([A-Z_][A-Z0-9_]*)=", path.read_text(), flags=re.M)
    script = 'set -a; source "$1"; for k in "${@:2}"; do printf "%s=%s\\0" "$k" "${!k}"; done'
    out = subprocess.run(
        ["bash", "-c", script, "_", str(path), *keys],
        env=os.environ if env is None else env,
        check=True, capture_output=True, text=True,
    ).stdout
    values = dict(item.split("=", 1) for item in out.split("\0") if item)
    return Config(values)

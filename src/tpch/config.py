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
        """tpch_sf<N>; with TPCH_DB_VARIANT=<v> in the environment, the variant database tpch_sf<N>_<v>
        (e.g. bigkeys: BIGINT order keys at a small SF, src/setup/variant_bigkeys.sh)."""
        v = os.environ.get("TPCH_DB_VARIANT", "")
        return f"tpch_sf{sf}_{v}" if v else f"tpch_sf{sf}"

    # --- volumes: SF < BIG_SF_MIN lives under DATA_ROOT (sda), larger SFs under BIG_ROOT (sdb) ---
    def big(self, sf: int) -> bool:
        return sf >= int(self.values.get("BIG_SF_MIN", "0") or 0) and "BIG_ROOT" in self.values

    def root(self, sf: int) -> str:
        """Root of the volume that holds this SF's tables (its bench/ dir takes the scratch files)."""
        return self.values["BIG_ROOT"] if self.big(sf) else self.values["DATA_ROOT"]

    def device(self, sf: int) -> str:
        """Block device sampled (iostat, /proc/diskstats) while this SF is measured."""
        return self.values["BIG_IO_DEVICE"] if self.big(sf) else self.values["IO_DEVICE"]

    def volume_of(self, path: str | Path) -> tuple[str, str]:
        """(root, device) of the volume a file lies on (symlinks such as pg_tblspc/ resolved)."""
        real = Path(path).resolve()
        big = self.values.get("BIG_ROOT")
        if big and real.is_relative_to(Path(big).resolve()):
            return big, self.values["BIG_IO_DEVICE"]
        return self.values["DATA_ROOT"], self.values["IO_DEVICE"]

    def volume(self, name: str) -> tuple[str, str]:
        """`data` (DATA_ROOT, sda) or `big` (BIG_ROOT, sdb) -> (root, device)."""
        if name == "big":
            return self.values["BIG_ROOT"], self.values["BIG_IO_DEVICE"]
        if name == "data":
            return self.values["DATA_ROOT"], self.values["IO_DEVICE"]
        raise ValueError(f"volume must be 'data' or 'big', not {name!r}")


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

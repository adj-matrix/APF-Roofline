"""System settings for measurements: changed when a measuring command starts, restored when it ends.

The machine is ours alone (2026-11-12), so system-wide settings may change, but only for the
duration of a measurement: nothing stays changed afterwards, so nothing is forgotten.

    SYSCONF="watchdog timers pgnuma"   (config.env; empty = touch nothing)

Knobs:
  watchdog  kernel.nmi_watchdog=0: the watchdog holds one of the 4 general perf counters
  timers    stop the periodic jobs in TIMERS (plocate-updatedb scans the disks every night, ...)
  pgnuma    restart PostgreSQL under `numactl` on PG_NUMA_NODE (the RAID controller's node, DDIO);
            every restart during the command (cold runs) stays bound; at the end it restarts unbound
  cpufreq   CPU_GOVERNOR on every CPU and intel_pstate/no_turbo=CPU_NO_TURBO (the baseline; the system
            default is schedutil with turbo). `./tpch experiment --cpufreq` also varies it per run.

`applied()` writes the original state to results/sysconf/active.json BEFORE changing anything and
deletes it after restoring. If a command is killed, that file remains: `./tpch sysconf restore`
undoes it, and the next measuring command also restores to it (it keeps the true originals).
"""
from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

from .config import Config
from .shell import Shell, root_prefix

KNOBS = ("watchdog", "timers", "pgnuma", "cpufreq")
TIMERS = ("plocate-updatedb.timer", "apt-daily.timer", "apt-daily-upgrade.timer", "man-db.timer",
          "fwupd-refresh.timer", "e2scrub_all.timer", "fstrim.timer", "motd-news.timer", "logrotate.timer",
          "dpkg-db-backup.timer", "systemd-tmpfiles-clean.timer", "update-notifier-download.timer",
          "update-notifier-motd.timer")
WATCHDOG = "/proc/sys/kernel/nmi_watchdog"
CPUFREQ = Path("/sys/devices/system/cpu")
NO_TURBO = "/sys/devices/system/cpu/intel_pstate/no_turbo"


def knobs(cfg: Config) -> list[str]:
    ks = cfg.values.get("SYSCONF", "").split()
    unknown = set(ks) - set(KNOBS)
    if unknown:
        raise ValueError(f"SYSCONF: unknown knobs {sorted(unknown)} (known: {KNOBS})")
    return ks


def pg_numa_node(cfg: Config) -> str | None:
    """The node the postmaster is started on, or None (unbound)."""
    return (cfg.values.get("PG_NUMA_NODE") or None) if "pgnuma" in knobs(cfg) else None


def _active(units: tuple[str, ...]) -> list[str]:
    r = subprocess.run(["systemctl", "is-active", *units], capture_output=True, text=True)
    return [u for u, s in zip(units, r.stdout.split()) if s == "active"]


def _pg_running(cfg: Config) -> bool:
    r = subprocess.run([f"{cfg.PGBIN}/pg_ctl", "-D", cfg.PGDATA, "status"], capture_output=True)
    return r.returncode == 0


def _read(path: str | Path) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _governor() -> str | None:
    """The governor of every CPU (several, comma-joined, if they differ)."""
    govs = sorted({_read(f) for f in CPUFREQ.glob("cpu[0-9]*/cpufreq/scaling_governor")} - {None})
    return ",".join(govs) or None


def state(cfg: Config | None = None) -> dict:
    """The settings the knobs change (also recorded in every run's meta.json via sysinfo)."""
    s = {"nmi_watchdog": _read(WATCHDOG), "timers_active": _active(TIMERS),
         "governor": _governor(), "no_turbo": _read(NO_TURBO)}
    if cfg is not None:
        s["pg_running"] = _pg_running(cfg)
    return s


def _active_path(cfg: Config) -> Path:
    return cfg.repo / cfg.RESULTS_DIR / "sysconf" / "active.json"


def _restart_pg(cfg: Config, sh: Shell, bind: bool) -> None:
    from .cache import pg_ctl
    pg_ctl(cfg, sh, "-m", "fast", "restart", bind=bind)


def parse_cpufreq(text: str) -> tuple[str, str]:
    """`performance:1` -> ("performance", "1"): governor and no_turbo (0 = turbo on)."""
    gov, _, nt = text.partition(":")
    if not gov or nt not in ("0", "1"):
        raise ValueError(f"cpufreq must be GOVERNOR:NO_TURBO, e.g. schedutil:0, not {text!r}")
    return gov, nt


def set_cpufreq(sh: Shell, governor: str, no_turbo: str) -> None:
    """Every CPU's governor and the turbo switch (one root shell command).

    Order matters (found 2026-11-14, cpufreq experiment): writing no_turbo 1 -> 0 under an unchanged
    `schedutil` left every CPU at <= 2.7 GHz (no turbo) although sysfs read no_turbo=0 and
    scaling_max_freq=3.5 GHz, until the governor was restarted. So no_turbo is written first and the
    governor is then restarted through another one, which makes it read the new limits."""
    other = "schedutil" if governor == "performance" else "performance"
    gov = f"{CPUFREQ}/cpu[0-9]*/cpufreq/scaling_governor"
    sh.run([*root_prefix(), "sh", "-c",
            f"echo {no_turbo} > {NO_TURBO}; "
            f"for f in {gov}; do echo {other} > $f; done; for f in {gov}; do echo {governor} > $f; done"])


def _baseline_cpufreq(cfg: Config) -> tuple[str, str]:
    return cfg.values.get("CPU_GOVERNOR", "schedutil"), cfg.values.get("CPU_NO_TURBO", "0")


def _apply(cfg: Config, sh: Shell, ks: list[str], orig: dict) -> None:
    if "watchdog" in ks and orig["nmi_watchdog"] not in (None, "0"):
        sh.run([*root_prefix(), "sysctl", "-q", "-w", "kernel.nmi_watchdog=0"])
    if "timers" in ks and orig["timers_active"]:
        sh.run([*root_prefix(), "systemctl", "stop", *orig["timers_active"]])
    if "pgnuma" in ks and orig.get("pg_running"):
        _restart_pg(cfg, sh, bind=True)
    if "cpufreq" in ks:     # always: a stale governor can read right in sysfs and still lack turbo
        set_cpufreq(sh, *_baseline_cpufreq(cfg))


def restore(cfg: Config, sh: Shell, orig: dict, ks: list[str] | tuple[str, ...] = KNOBS) -> None:
    """Undo `_apply`. Every step runs even if an earlier one fails; the first error is raised at the end."""
    steps = []
    if "watchdog" in ks and orig.get("nmi_watchdog") not in (None, "0"):
        steps.append(lambda: sh.run([*root_prefix(), "sysctl", "-q", "-w",
                                     f"kernel.nmi_watchdog={orig['nmi_watchdog']}"]))
    if "timers" in ks and orig.get("timers_active"):
        steps.append(lambda: sh.run([*root_prefix(), "systemctl", "start", *orig["timers_active"]]))
    if "pgnuma" in ks and orig.get("pg_running") and (sh.dry_run or _pg_running(cfg)):
        steps.append(lambda: _restart_pg(cfg, sh, bind=False))
    # Always rewritten (not only when sysfs differs): sysfs can read right while turbo is still off.
    if "cpufreq" in ks and orig.get("governor") and "," not in orig["governor"] and orig.get("no_turbo"):
        steps.append(lambda: set_cpufreq(sh, orig["governor"], orig["no_turbo"]))
    errors = []
    for step in steps:
        try:
            step()
        except Exception as e:  # noqa: BLE001 - restore as much as possible
            errors.append(e)
    if errors:
        raise errors[0]


@contextmanager
def applied(cfg: Config, sh: Shell):
    """Apply SYSCONF for the duration of the block, then restore. Yields the original state."""
    ks = knobs(cfg)
    if not ks:
        yield {}
        return
    path = _active_path(cfg)
    if path.exists():   # a previous command was killed: these are the true originals
        doc = json.loads(path.read_text())
        orig, ks = doc["orig"], sorted(set(ks) | set(doc["knobs"]))
    else:
        orig = state(cfg)
    if not sh.dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"knobs": ks, "orig": orig}, indent=2))
    try:
        _apply(cfg, sh, ks, orig)
        yield orig
    finally:
        restore(cfg, sh, orig, ks)
        if not sh.dry_run:
            path.unlink(missing_ok=True)


def restore_interrupted(cfg: Config, sh: Shell) -> dict | None:
    """`./tpch sysconf restore`: undo a killed command's settings, if any."""
    path = _active_path(cfg)
    if not path.exists():
        return None
    doc = json.loads(path.read_text())
    restore(cfg, sh, doc["orig"], doc["knobs"])
    if not sh.dry_run:
        path.unlink()
    return doc

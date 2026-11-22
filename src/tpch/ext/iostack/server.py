"""Set or reset the postmaster-level I/O settings of E1 and restart PostgreSQL (the machine is ours alone).

  python -m tpch.ext.iostack.server set METHOD DIRECT [NAME=VALUE ...]
                                    io_method = METHOD (sync|worker|io_uring), debug_io_direct = '' or 'data'
                                    (DIRECT = off|data); optionally io_workers / effective_io_concurrency
  python -m tpch.ext.iostack.server reset                back to the configured defaults
Both use ALTER SYSTEM (postgresql.auto.conf) and a restart, then print what the server reports.
"""
import sys

import psycopg

from ...cache import pg_ctl
from ...config import load
from ...shell import Shell


def _sql(cfg, statements):
    with psycopg.connect(host=cfg.PGSOCK, port=cfg.PG_PORT, dbname="postgres", autocommit=True) as conn:
        for s in statements:
            conn.execute(s)


def show(cfg) -> dict:
    with psycopg.connect(host=cfg.PGSOCK, port=cfg.PG_PORT, dbname="postgres", autocommit=True) as conn:
        return dict(conn.execute("SELECT name, setting FROM pg_settings WHERE name = ANY(%s)",
                                 (["io_method", "debug_io_direct", "io_workers", "effective_io_concurrency",
                                   "io_combine_limit"],)).fetchall())


EXTRA = ("io_workers", "effective_io_concurrency")


def main(argv: list[str]) -> None:
    cfg = load()
    extra = dict(a.split("=", 1) for a in argv[3:]) if argv[:1] == ["set"] else {}
    if argv[:1] == ["set"] and len(argv) >= 3 and argv[1] in ("sync", "worker", "io_uring") \
            and argv[2] in ("off", "data") and set(extra) <= set(EXTRA) and all(v.isdigit() for v in extra.values()):
        direct = "" if argv[2] == "off" else "data"
        _sql(cfg, [f"ALTER SYSTEM SET io_method = '{argv[1]}'", f"ALTER SYSTEM SET debug_io_direct = '{direct}'"]
             + [f"ALTER SYSTEM SET {k} = {extra[k]}" if k in extra else f"ALTER SYSTEM RESET {k}" for k in EXTRA])
    elif argv == ["reset"]:
        _sql(cfg, ["ALTER SYSTEM RESET io_method", "ALTER SYSTEM RESET debug_io_direct"]
             + [f"ALTER SYSTEM RESET {k}" for k in EXTRA])
    else:
        sys.exit(__doc__)
    sh = Shell(False)
    pg_ctl(cfg, sh, "-m", "fast", "restart")
    print(" ".join(f"{k}={v!r}" for k, v in sorted(show(cfg).items())), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])

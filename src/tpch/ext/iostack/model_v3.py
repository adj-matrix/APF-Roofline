"""E1 model v3 = v2 with PostgreSQL's own scans as the replica where the C replica misses PostgreSQL's dynamics
(docs/ext/iostack.md, "Fix", M8–M10). Written before M10's data was looked at.

The scan family (queries/scan/micro.sql: lineitem_min / lineitem_light / lineitem_mid; no TPC-H query) runs over
all of lineitem under a cell's io_method / io_direct. Each run gives, per 128 KiB read: the backend's user cycles
u, kernel cycles k, clock f, and the time per read off the CPU, off = (wall - W_start) / n - (u + k) / f. For a
query in that cell, at the query's own user cycles per read u_q (as in v2: U / n self-completing, U / n - dU in
worker mode):
  f, off, k   read from the family at u_q (linear in u, medians per scan); outside the family's u range the cell
              keeps v2 (the C replica)
  sync / data u_q = U / n - du_direct, du_direct = u(light, sync / off) - u(light, sync / data): the backend's user
              cycles per read that a buffered read costs more than a direct one (M8 / M9 light scans; additive per
              read)
  T           n * ((u_q + k) / f + off) + W_start
Cells: worker / off and sync / data (the two v2 gaps). The others are v2's.
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics as st
from pathlib import Path

from ...config import load as load_config
from ...model.params import ParamSet, interp
from .model_v2 import READ_MIB, SEEN, du_per_read, latest_m7, load_replica, predict_cells

CELLS = {("worker", "off"), ("sync", "data")}
EXCLUDE = {"20261122-153236"}         # M8 block 1 worker / off: overlapped my calibration read on sdb (docs, M8)


def load_scans(cfg) -> dict:
    """{(method, direct): {scan: [run rows]}} from the M8-M10 scan runs (labels iostack_m{8,9,10}_METHOD_DIRECT)."""
    out: dict = {}
    for d in sorted(glob.glob(str(cfg.repo / cfg.RESULTS_DIR / "runs" / "*_scan.lineitem_*_sf100_plain_cold_iostack_m*"))):
        name = Path(d).name
        if name[:15] in EXCLUDE:
            continue
        s = json.loads((Path(d) / "summary.json").read_text())
        scan = name.split("_scan.")[1].split("_sf100")[0]
        lab = s["label"].split("_", 2)[2]                       # METHOD_DIRECT (io_uring has one more _)
        method, direct = lab.rsplit("_", 1)
        n = s["dev_read_MiB"] / READ_MIB
        u = s["perf_cycles"] * s["perf_user_cycles_frac"] / n
        k = s["perf_cycles"] * (1 - s["perf_user_cycles_frac"]) / n
        f = s["perf_GHz"] * 1e9
        out.setdefault((method, direct), {}).setdefault(scan, []).append(
            {"u": u, "k": k, "f": f, "wall": s["wall_s"], "n": n, "dir": name})
    return out


def family(scans: dict, start: float) -> list[dict]:
    """One point per scan (medians over its runs), sorted by u, with off per read."""
    pts = []
    for scan, rows in scans.items():
        m = {key: st.median(r[key] for r in rows) for key in ("u", "k", "f", "wall", "n")}
        m["off"] = (m["wall"] - start) / m["n"] - (m["u"] + m["k"]) / m["f"]
        m["scan"], m["runs"] = scan, len(rows)
        pts.append(m)
    return sorted(pts, key=lambda p: p["u"])


def predict(cfg, params, query: str, sf: int, cells) -> dict:
    rep = load_replica(latest_m7(cfg))
    du = du_per_read(cfg, query)["dU"]
    v2 = predict_cells(cfg, params, query, sf, rep, du, cells)
    b = v2["base"]
    n, start = b["n_reads"], b["start_s"]
    scans = load_scans(cfg)
    light = {c: st.median(r["u"] for r in scans.get(c, {}).get("lineitem_light", [{"u": float("nan")}]))
             for c in (("sync", "off"), ("sync", "data"))}
    du_direct = light[("sync", "off")] - light[("sync", "data")]
    for c in v2["cells"]:
        key = (c["io_method"], c["io_direct"])
        if key not in CELLS or c["extra"] or key not in scans:
            c["model"] = "v2"
            continue
        uq = b["U"] / n - (du if key[0] == "worker" else du_direct)
        fam = family(scans[key], start)
        if not fam[0]["u"] <= uq <= fam[-1]["u"]:
            c["model"] = f"v2 (u {uq / 1e3:.0f}k outside the scan family {fam[0]['u'] / 1e3:.0f}k-{fam[-1]['u'] / 1e3:.0f}k)"
            continue
        at = lambda key_: interp([[p["u"], p[key_]] for p in fam], uq, log_x=False)
        f, off, k = at("f"), at("off"), at("k")
        c.update(model="v3", time_s=n * ((uq + k) / f + off) + start, user_cycles=n * uq, kernel_cycles=n * k,
                 cycles=n * (uq + k), clock_ghz=f / 1e9, off_cpu_s=n * off,
                 note=f"scan family {', '.join(p['scan'] + ' ' + format(p['u'] / 1e3, '.0f') + 'k' for p in fam)}: "
                      f"f {f / 1e9:.3f}, k/read {k / 1e3:.1f}k, off/read {off * 1e6:.1f} us")
    v2["du_direct_per_read"] = du_direct
    v2["scan_runs"] = {f"{m}/{d}": {s: [r["dir"] for r in rows] for s, rows in v.items()} for (m, d), v in scans.items()}
    return v2


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--query", required=True)
    ap.add_argument("--sf", type=int, default=100)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    cfg = load_config()
    params = ParamSet.load(cfg.repo / "model" / "params.json")
    res = predict(cfg, params, a.query, a.sf, SEEN[a.query])
    print(f"du_direct {res['du_direct_per_read'] / 1e3:.1f}k per read")
    for c in res["cells"]:
        print(f"{c['io_method']:9s} {c['io_direct']:5s} {c['model'][:3]}  T {c['time_s']:7.1f}  U {c['user_cycles'] / 1e9:6.1f}G "
              f"K {c['kernel_cycles'] / 1e9:5.1f}G  f {c['clock_ghz']:.3f}  off {c['off_cpu_s']:5.1f}  {c['note']}")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(res, indent=1))
        print("prediction ->", a.out)


if __name__ == "__main__":
    main()

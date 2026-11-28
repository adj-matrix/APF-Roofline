"""CLI: python -m tpch <command>   (or ./tpch <command> from the repo root)"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from contextlib import nullcontext

from . import config, sysconf, sysinfo
from .shell import Shell
from .runner import CACHES, MODES, run_query
from .summarize import summarize


def _kv(s: str) -> tuple[str, str]:
    k, sep, v = s.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got {s!r}")
    return k, v


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tpch")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="measure a query; one run directory per repetition")
    r.add_argument("--query", required=True, help="query directory under queries/, e.g. q6")
    r.add_argument("--sf", type=int, required=True)
    r.add_argument("--cache", choices=CACHES, default="cold")
    r.add_argument("--mode", choices=list(MODES), default="plain")
    r.add_argument("--repeat", type=int, default=3)
    r.add_argument("--label", default="")
    r.add_argument("--set", type=_kv, action="append", default=[], metavar="NAME=VALUE",
                   help="PostgreSQL setting for the query's connection, e.g. --set work_mem=256MB (repeatable)")
    r.add_argument("--dry-run", action="store_true", help="print what would run; touch nothing")

    se = sub.add_parser("experiment", help="randomized block design: several conditions, interleaved")
    se.add_argument("--query", nargs="+", required=True,
                    help="one query, or several parts of one (q1.m_scan q1.m_filter ...: a factor of the design)")
    se.add_argument("--sf", type=int, nargs="+", required=True)
    se.add_argument("--cache", choices=CACHES, nargs="+", default=list(CACHES))
    se.add_argument("--mode", choices=list(MODES), nargs="+", default=["plain"])
    se.add_argument("--cpufreq", nargs="+", metavar="GOV:NO_TURBO",
                    help="CPU frequency as a factor, set before each run, e.g. schedutil:0 performance:0 schedutil:1")
    se.add_argument("--blocks", type=int, default=3, help="each block runs every condition once, shuffled")
    se.add_argument("--seed", type=int, help="shuffle seed (default: random, recorded)")
    se.add_argument("--label", default="")
    se.add_argument("--set", type=_kv, action="append", default=[], metavar="NAME=VALUE")
    se.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("summarize", help="(re)build timeline.csv + summary.json of run dirs")
    s.add_argument("runs", nargs="+", type=Path)

    b = sub.add_parser("bench", help="microbenchmarks")
    bsub = b.add_subparsers(dest="bench", required=True)
    bi = bsub.add_parser("io", help="fio read sweep on a dedicated file under <volume root>/bench")
    bi.add_argument("--volume", choices=["data", "big"], default="data",
                    help="data: DATA_ROOT (sda); big: BIG_ROOT (sdb, SF >= BIG_SF_MIN)")
    bi.add_argument("--file-gib", type=int, default=16)
    bi.add_argument("--runtime", type=int, default=15, help="seconds per test")
    bi.add_argument("--dry-run", action="store_true")
    bm = bsub.add_parser("mem", help="dependent random-access latency vs working-set size (pointer chase)")
    bm.add_argument("--reps", type=int, default=3)
    bm.add_argument("--dry-run", action="store_true")
    bl = bsub.add_parser("location", help="O_DIRECT read bandwidth vs physical position (after controller-cache eviction)")
    bl.add_argument("--table", nargs="+", default=[], metavar="SF:TABLE", help="e.g. 1:lineitem 10:orders")
    bl.add_argument("--file", nargs="+", default=[], help="extra files (read only)")
    bl.add_argument("--no-default", action="store_true", help="skip the fio file + lineitem of every loaded SF")
    bl.add_argument("--dry-run", action="store_true")

    rp = sub.add_parser("report", help="figures + numbers.json of one report directory (its figures.py)")
    rp.add_argument("cell", help="a cell under queries/ (e.g. q6/sf10), or 'hardware'")
    rp.add_argument("--cond", type=_kv, action="append", default=[], metavar="NAME=LABEL",
                    help="condition name and run label, e.g. --cond A=v1 (repeatable, ordered)")
    rp.add_argument("--input", type=_kv, action="append", default=[], metavar="NAME=VALUE[,VALUE...]",
                    help="results the figures are built from, e.g. --input scan=<bench run> (repeatable)")
    rp.add_argument("--out", type=Path, help="output directory (default: the cell directory)")
    rp.add_argument("--timeline", nargs="+", help="condition names drawn in the timeline figure (max 3)")

    mo = sub.add_parser("model", help="the performance model: calibrate, predict, evaluate (src/tpch/model)")
    msub = mo.add_subparsers(dest="model", required=True)
    mc = msub.add_parser("calibrate", help="estimate every parameter from benchmarks and training runs")
    mc.add_argument("--out", type=Path, default=Path("model/params.json"))
    mp = msub.add_parser("predict", help="predict one condition (EXPLAIN only, nothing is executed)")
    mp.add_argument("--query", required=True)
    mp.add_argument("--sf", type=int, required=True)
    mp.add_argument("--set", type=_kv, action="append", default=[], metavar="NAME=VALUE")
    mp.add_argument("--params", type=Path, default=Path("model/params.json"))
    mp.add_argument("--out", type=Path, help="write the prediction (JSON) here, e.g. model/predictions/<name>.json")
    me = msub.add_parser("evaluate", help="replay the registered conditions; one row per condition")
    me.add_argument("--params", type=Path, default=Path("model/params.json"))
    me.add_argument("--baseline", type=Path, metavar="EVALUATION",
                    help="an earlier version's model/evaluations/<digest>.json: compare condition by condition "
                         "(regression check)")

    sub.add_parser("sysinfo", help="print the environment snapshot")
    sc = sub.add_parser("sysconf", help="system settings changed during measurements (SYSCONF in config.env)")
    sc.add_argument("action", choices=["show", "restore"],
                    help="show: current state; restore: undo the settings of a killed command")

    a = p.parse_args(argv)
    # Measuring commands change SYSCONF's settings when they start and restore them when they end.
    measuring = a.cmd in ("run", "experiment", "bench")
    sys_sh = Shell(getattr(a, "dry_run", False))
    with sysconf.applied(config.load(), sys_sh) if measuring else nullcontext():
        _dispatch(a)
    if measuring and sys_sh.log:
        print("system settings (applied, then restored):\n  " + "\n  ".join(sys_sh.log))
    return 0


def _dispatch(a: argparse.Namespace) -> None:
    if a.cmd == "run":
        cfg = config.load()
        runs, sh = run_query(cfg, a.query, a.sf, a.cache, a.mode, a.label, a.repeat,
                             dict(a.set), dry_run=a.dry_run)
        if a.dry_run:
            print("\n".join(sh.log))
        for run in runs:
            if not a.dry_run:
                sm = json.loads((run / "summary.json").read_text())
                print(f"{run.name}: wall {sm['wall_s']:.2f}s  read {sm['dev_read_MiB']:.0f} MiB  "
                      f"backend CPU {sm['backend_cpu_util'] or 0:.0%}")
    elif a.cmd == "experiment":
        from .experiment import conditions, run_experiment
        conds = conditions(a.sf, a.cache, a.mode, a.query, a.cpufreq)
        cfg = config.load()
        path, sh = run_experiment(cfg, a.query[0], conds, a.blocks, a.seed, a.label,
                               dict(a.set), dry_run=a.dry_run)
        if a.dry_run:
            print("\n".join(sh.log))
        else:
            doc = json.loads(path.read_text())
            for o in doc["order"]:
                sm = json.loads((cfg.repo / cfg.RESULTS_DIR / "runs" / o["run"] / "summary.json").read_text())
                print(f"{o['position']:3d} b{o['block']} {o.get('query', ''):<14s} sf{o['sf']:<3d} {o['mode']:<8s} {o['cache']:<4s} "
                      f"{o.get('cpufreq', ''):<14s} "
                      f"wall {sm['wall_s']:7.2f}s  read {sm['dev_read_MiB']:7.0f} MiB  (prev {o['prev']})")
            print(f"experiment file: {path}")
    elif a.cmd == "summarize":
        for run in a.runs:
            summarize(run)
            print(f"summarized {run}")
    elif a.cmd == "bench" and a.bench == "io":
        from .bench.io import GiB, run_io_bench
        out, sh = run_io_bench(config.load(), file_size=a.file_gib * GiB, runtime_s=a.runtime,
                               dry_run=a.dry_run, volume=a.volume)
        print("\n".join(sh.log) if a.dry_run else f"results in {out}")
    elif a.cmd == "bench" and a.bench == "mem":
        from .bench.mem import run_mem_bench
        out, sh = run_mem_bench(config.load(), reps=a.reps, dry_run=a.dry_run)
        print("\n".join(sh.log) if a.dry_run else f"results in {out}")
    elif a.cmd == "bench" and a.bench == "location":
        from .bench.location import default_targets, relation_files, run_location_bench
        cfg = config.load()
        targets = [] if a.no_default else default_targets(cfg)
        for t in a.table:
            sf, table = t.split(":", 1)
            targets.append((f"sf{sf}.{table}", relation_files(cfg, int(sf), table)))
        targets += [(Path(f).name, [f]) for f in a.file]
        out, sh = run_location_bench(cfg, targets, dry_run=a.dry_run)
        print("\n".join(sh.log) if a.dry_run else f"results in {out}")
    elif a.cmd == "report":
        import importlib
        cfg = config.load()
        cell = a.cell.strip("/")
        rel = Path(cell) if (cfg.repo / cell / "figures.py").exists() else Path("queries") / cell
        figures = importlib.import_module(".".join(rel.parts) + ".figures")
        a.out = a.out or cfg.repo / rel
        inputs: dict[str, list[str]] = {}
        for name, value in a.input:
            inputs.setdefault(name, []).extend(value.split(","))
        nums = figures.build(cfg, dict(a.cond), a.out, a.timeline, inputs)
        for name, c in nums.get("conditions", {}).items():
            print(f"{name} ({c['label']}): cold {c['cold_wall_s']['median']:.2f}s  "
                  f"predicted [{c['prediction']['lower_s']:.1f}, {c['prediction']['upper_s']:.1f}]  "
                  f"warm {c['warm_wall_s']['median']:.2f}s")
        print(f"figures + numbers.json in {a.out}")
    elif a.cmd == "model":
        _model(a)
    elif a.cmd == "sysinfo":
        print(json.dumps(sysinfo.collect(), indent=2))
    elif a.cmd == "sysconf":
        cfg = config.load()
        if a.action == "show":
            print(json.dumps({"SYSCONF": sysconf.knobs(cfg), **sysconf.state(cfg)}, indent=2))
        else:
            sh = Shell()
            doc = sysconf.restore_interrupted(cfg, sh)
            print("nothing to restore" if doc is None else "restored:\n  " + "\n  ".join(sh.log))


def _model(a: argparse.Namespace) -> None:
    from .model import calibrate, evaluate
    from .model.params import ParamSet
    cfg = config.load()
    rel = lambda p: p if p.is_absolute() else cfg.repo / p  # noqa: E731
    if a.model == "calibrate":
        params = calibrate.calibrate(cfg)
        params.save(rel(a.out))
        for name in sorted(params):
            value = params[name].value
            print(f"{name:38s} {params[name].kind:6s} {value if isinstance(value, list) else f'{value:.5g}'}"
                  f"  [{params[name].unit}]")
        print(f"params {params.digest()} -> {rel(a.out)}")
        return
    params = ParamSet.load(rel(a.params))
    if a.model == "predict":
        import subprocess
        pr = evaluate.predict_condition(cfg, params, a.query, a.sf, dict(a.set))
        doc = pr.as_dict()
        doc["model_commit"] = subprocess.run(["git", "-C", str(cfg.repo), "rev-parse", "HEAD"],
                                             capture_output=True, text=True).stdout.strip()
        low, high = pr.time_range_s
        print(f"{a.query} SF{a.sf} {dict(a.set) or ''}: T = {pr.time_s:.1f} s  [{low:.0f}, {high:.0f}]"
              f"  C = {pr.cycles / 1e9:.1f} G  f = {pr.clock_ghz:.3f} GHz  on CPU {pr.on_cpu_share:.1%}"
              f"  W = {pr.wait_s:.1f} s")
        for k, v in pr.parts.items():
            print(f"  {k:18s} {v / 1e9:.2f} G" if k.endswith("cycles") else f"  {k:18s} {v:.3f}")
        if a.out:
            rel(a.out).parent.mkdir(parents=True, exist_ok=True)
            rel(a.out).write_text(json.dumps(doc, indent=1, default=str) + "\n")
            print(f"prediction -> {rel(a.out)}")
    elif a.model == "evaluate":
        rows = evaluate.evaluate(cfg, params)
        print(evaluate.table(rows))
        print(f"params {params.digest()}")
        snap = rel(Path("model/evaluations") / f"{params.digest()}.json")
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(json.dumps(evaluate.snapshot(rows, params), indent=1) + "\n")
        print(f"evaluation -> {snap}")
        if a.baseline:
            baseline = json.loads(rel(a.baseline).read_text())
            print(f"\nregression check against params {baseline['params_digest']} ({a.baseline}):\n")
            print(evaluate.compare(rows, baseline))


if __name__ == "__main__":
    sys.exit(main())

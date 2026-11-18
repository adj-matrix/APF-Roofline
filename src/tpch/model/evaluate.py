"""Replay the model on registered conditions and compare every term with the measurement."""
from __future__ import annotations

import re
import statistics as st

from ..config import Config
from . import data, workload
from .cost import Prediction, predict
from .params import ParamSet
from .plan import Node, from_explain
from .registry import CALIBRATION, DATASETS, TESTS


def server_defaults(cfg: Config) -> dict:
    """work_mem and hash_mem_multiplier as the cluster is configured (src/setup/postgresql.tpch.conf).

    Returns:
        {"work_mem": ..., "hash_mem_multiplier": ...} as strings; PostgreSQL's defaults where the file
        does not set them.
    """
    text = (cfg.repo / "src" / "setup" / "postgresql.tpch.conf").read_text()
    out = {"work_mem": "4MB", "hash_mem_multiplier": "2.0"}
    for name in out:
        m = re.search(rf"^\s*{name}\s*=\s*([^\s#]+)", text, re.M)
        if m:
            out[name] = m.group(1).strip("'")
    return out


def workload_for(cfg: Config, params: ParamSet, query: str, sf: int, settings: dict,
                 target: Node) -> workload.Workload:
    """The workload of `query` at `sf` under `settings`, from its target plan and its scale model.

    Args:
        cfg: configuration.
        params: parameters (for the query's temp_bytes_ratio, if it has one).
        query: "q6", "q3", ...
        sf: target scale factor.
        settings: settings on top of the server's (e.g. {"work_mem": "64MB"}).
        target: the target plan (EXPLAIN, estimates only).
    """
    spec = CALIBRATION[query]
    cal_plan = data.load(cfg, spec["plan"], "calibrate")[0].plan
    effective = {**server_defaults(cfg), **settings}
    ratio_name = f"{query}.temp_bytes_ratio"
    ratio = params.value(ratio_name) if ratio_name in params else 1.0
    return workload.build(query, target, cal_plan, sf, spec["sf"], workload.catalog(cfg, sf), effective, ratio)


def predict_condition(cfg: Config, params: ParamSet, query: str, sf: int, settings: dict,
                      target: Node | None = None) -> Prediction:
    """Predict one condition; the target plan is EXPLAINed (and cached) unless given."""
    if target is None:
        target = from_explain(workload.explain(cfg, query, sf, settings))
    return predict(workload_for(cfg, params, query, sf, settings, target), params)


def observe(cfg: Config, dataset: str, plan_dataset: str | None, spilling: bool = True) -> dict:
    """The measured counterparts of a Prediction's terms: medians over the runs of `dataset`.

    Args:
        cfg: configuration.
        dataset: registered dataset with the measured runs.
        plan_dataset: registered dataset with an EXPLAIN ANALYZE of the same condition (batches, temp
            blocks), or None (no batches / temp blocks observed).
        spilling: the plan spills to temp files; adds the phase terms.

    Returns:
        {"n", "time_s", "cycles", "user_cycles", "kernel_cycles", "clock_ghz", "on_cpu_share", "wait_s",
         "nbatch", "temp_hj_blocks"} and, for spilling plans, the phase terms "wait_p12_s", "wait_p3_s",
         "len_p3_s", "temp_dev_MiB", and where the runs have per-second cycles, "clock_p12_ghz" and
         "clock_p3_ghz".
    """
    runs = data.load(cfg, dataset, "evaluate")

    def median(f) -> float:
        return st.median(f(r) for r in runs)

    obs = {"n": len(runs), "time_s": median(lambda r: r.wall), "cycles": median(lambda r: r.cycles),
           "user_cycles": median(lambda r: r.user_cycles), "kernel_cycles": median(lambda r: r.kernel_cycles),
           "clock_ghz": median(lambda r: r.ghz), "on_cpu_share": median(lambda r: r.on_cpu),
           "wait_s": median(lambda r: r.off_cpu)}
    obs["nbatch"], obs["temp_hj_blocks"] = [], None
    if plan_dataset:
        plan = data.load(cfg, plan_dataset, "evaluate")[0].plan
        obs["nbatch"] = [n.raw.get("Hash Batches") for n in plan.walk() if n.type == "Hash"]
        join_blocks = [n.raw.get("Temp Written Blocks", 0) for n in plan.walk() if n.type == "Hash Join"]
        obs["temp_hj_blocks"] = max(join_blocks) if join_blocks else 0
    if spilling:
        phases = [r.phases for r in runs]
        obs.update(wait_p3_s=st.median(p["off_p3_s"] for p in phases),
                   len_p3_s=st.median(p["p3_len_s"] for p in phases),
                   temp_dev_MiB=st.median(p["p3_dev_read_MiB"] for p in phases),
                   wait_p12_s=st.median(p["off_p12_s"] for p in phases))
        clocks = [r.phase_clocks for r in runs if r.phase_clocks]
        if clocks:
            obs.update(clock_p12_ghz=st.median(c[0] for c in clocks), clock_p3_ghz=st.median(c[1] for c in clocks))
    return obs


def evaluate(cfg: Config, params: ParamSet) -> list[dict]:
    """Predict every condition in registry.TESTS and pair it with its observation.

    Returns:
        [{"dataset", "role", "pred": Prediction, "obs": observe(...)}, ...] in TESTS order.
    """
    rows = []
    for dataset, plan_dataset, settings in TESTS:
        runs = data.load(cfg, dataset, "evaluate")
        query, sf = runs[0].meta["query"], runs[0].meta["sf"]
        target = from_explain(workload.explain(cfg, query, sf, settings))     # EXPLAIN, no execution
        pred = predict(workload_for(cfg, params, query, sf, settings, target), params)
        rows.append({"dataset": dataset, "role": DATASETS[dataset].role, "pred": pred,
                     "obs": observe(cfg, dataset, plan_dataset, spilling=bool(pred.workload["temp_bytes"]))})
    return rows


def snapshot(rows: list[dict], params: ParamSet) -> dict:
    """evaluate()'s rows as a JSON-able record: the baseline of a later regression check.

    Saved per model version (model/evaluations/<digest>.json), so that a new model version (new code and
    new parameters) is compared with what the old version predicted, not re-run with the old parameters.
    """
    return {"params_digest": params.digest(),
            "conditions": {r["dataset"]: {"role": r["role"], "time_s": r["pred"].time_s,
                                          "measured_time_s": r["obs"]["time_s"]} for r in rows}}


def compare(new: list[dict], baseline: dict, tolerance_pt: float = 1.0) -> str:
    """Regression check: the time error of each condition, now and in a baseline snapshot.

    Args:
        new: evaluate() rows of the current model version.
        baseline: snapshot() of an earlier version.
        tolerance_pt: a condition is flagged "worse" when |error| grows by more than this many points.

    Returns:
        A markdown table, one row per condition (conditions new since the baseline show "new"), and a
        last line counting better / same / worse.
    """
    out = ["| dataset | role | T meas | T old | err old | T new | err new | |",
           "|---|---|---|---|---|---|---|---|"]
    tally = {"better": 0, "same": 0, "worse": 0, "new": 0}
    for n in new:
        meas = n["obs"]["time_s"]
        err_new = (n["pred"].time_s / meas - 1) * 100
        old = baseline["conditions"].get(n["dataset"])
        if old is None:
            tally["new"] += 1
            out.append(f"| {n['dataset']} | {n['role']} | {meas:.1f} | - | - | {n['pred'].time_s:.1f} | "
                       f"{err_new:+.1f}% | new |")
            continue
        err_old = (old["time_s"] / meas - 1) * 100
        verdict = ("worse" if abs(err_new) > abs(err_old) + tolerance_pt else
                   "better" if abs(err_new) < abs(err_old) - tolerance_pt else "same")
        tally[verdict] += 1
        out.append(f"| {n['dataset']} | {n['role']} | {meas:.1f} | {old['time_s']:.1f} | {err_old:+.1f}% | "
                   f"{n['pred'].time_s:.1f} | {err_new:+.1f}% | {'**worse**' if verdict == 'worse' else verdict} |")
    out.append(f"\n{tally['better']} better, {tally['same']} same (within {tolerance_pt} pt), {tally['worse']} worse"
               + (f", {tally['new']} new" if tally["new"] else ""))
    return "\n".join(out)


def table(rows: list[dict]) -> str:
    """evaluate()'s rows as a markdown table: error of T and of each term (+ = prediction too high)."""
    def err(pred: float, obs: float) -> str:
        return f"{(pred / obs - 1) * 100:+.1f}%" if obs else "-"

    out = ["| dataset | role | T pred | T meas | err | C err | U err | K err | f pred / meas | W pred / meas "
           "| batches pred / meas | temp blocks pred / meas |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        pred, obs = row["pred"], row["obs"]
        joins = pred.workload["joins"]
        batches_pred = "/".join(str(j["nbatch"]) for j in reversed(joins)) or "-"
        batches_obs = "/".join(str(x) for x in obs["nbatch"]) or "-"
        blocks_obs = "-" if obs["temp_hj_blocks"] is None else f"{obs['temp_hj_blocks']:,}"
        temp_blocks_pred = sum(j["temp_bytes"] for j in joins) / 8192
        low, high = pred.time_range_s
        out.append(
            f"| {row['dataset']} | {row['role']} | {pred.time_s:.1f} [{low:.0f}, {high:.0f}] | {obs['time_s']:.1f} | "
            f"**{err(pred.time_s, obs['time_s'])}** | {err(pred.cycles, obs['cycles'])} | "
            f"{err(pred.user_cycles, obs['user_cycles'])} | {err(pred.kernel_cycles, obs['kernel_cycles'])} | "
            f"{pred.clock_ghz:.3f} / {obs['clock_ghz']:.3f} | {pred.wait_s:.1f} / {obs['wait_s']:.1f} | "
            f"{batches_pred} / {batches_obs} | {temp_blocks_pred:,.0f} / {blocks_obs} |")
    return "\n".join(out)

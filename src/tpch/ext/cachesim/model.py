"""E4 model: the base model with the page cache's capacity and turnover (docs/ext/cachesim.md, M1-M3).

Two changes, both set from microbenchmarks, none fitted on a query:
  capacity   page_cache_window_MiB = the bound node's file-page capacity C (M1), minus any memory pinned on that
             node (the balloon in M2). The base LRU formula for the data read back from the device is unchanged.
  turnover   a page reaches the tail of the inactive list after t_tail = k x C / R, R = MiB/s entering the cache
             in phases 1-2 (tables read + temp written) / phase 1-2 time; reclaim then wakes the flusher. Write-
             back therefore starts after min(expiry, t_tail) (M3), which
               - cuts each batch file into chunks of r x min(expiry, t_tail) instead of r x expiry (extents,
                 read-back seeks), and
               - lengthens the write-back bursts' device time per GiB, from the base value at t_tail >= expiry
                 to x BURST_RATIO at M3's t_tail (M3: 1.52 -> 2.11 s per GiB at 11.4 s), linear in between.
             k = 0.83: the mean of C / R over the measured t_tail in M3 (b24: 0.89, b16: 0.77).
The base model's code and parameters are not changed: cost.batch_file_extents is replaced only for the duration
of one prediction (`predict`), and the parameters come from model/ext/cachesim/params.json.
"""
from __future__ import annotations

import contextlib
import copy
import json
from pathlib import Path

from ...model import cost
from ...model.cost import MiB, extents_per_file
from ...model.evaluate import predict_condition
from ...model.params import ParamSet

K_TAIL = 0.83
M3_TAIL_S, BURST_RATIO = 11.4, 2.11 / 1.52


@contextlib.contextmanager
def tail_limited_extents(t_tail: float):
    """cost.batch_file_extents with chunk = r x min(expiry, t_tail), for the duration of the block."""
    original = cost.batch_file_extents

    def extents(join, params, inner_write_s, outer_write_s):
        files = join.nbatch - 1
        expiry, window, start = (params.value("writeback_expire_s"), params.value("mballoc_window_MiB"),
                                 params.value("start_extents_vs_chunk_MiB"))
        total = 0.0
        for side_bytes, seconds in ((join.temp_bytes - join.outer_temp_bytes, inner_write_s),
                                    (join.outer_temp_bytes, outer_write_s)):
            size = side_bytes / MiB / files
            chunk = min(size, size / max(seconds, 1e-9) * min(expiry, t_tail))
            total += files * extents_per_file(size, chunk, window, start)
        return total

    cost.batch_file_extents = extents
    try:
        yield
    finally:
        cost.batch_file_extents = original


def params_with(base: dict, **values) -> ParamSet:
    p = copy.deepcopy(base)
    for x in p["params"]:
        if x["name"] in values:
            x["value"] = values[x["name"]]
    path = Path("/dev/shm") / f"cachesim_params_{abs(hash(json.dumps(values, sort_keys=True)))}.json"
    path.write_text(json.dumps(p))
    try:
        return ParamSet.load(path)
    finally:
        path.unlink(missing_ok=True)


def predict(cfg, query: str, sf: int, settings: dict, pinned_gib: float = 0.0,
            params_file: Path = Path("model/ext/cachesim/params.json")) -> dict:
    base = json.loads(Path(params_file).read_text())
    value = {x["name"]: x["value"] for x in base["params"]}
    capacity = value["page_cache_window_MiB"] - pinned_gib * 1024
    expiry = value["writeback_expire_s"]
    first = predict_condition(cfg, params_with(base, page_cache_window_MiB=capacity), query, sf, settings)
    parts = first.parts
    if not parts.get("temp_MiB"):
        return {"capacity_MiB": capacity, "t_tail_s": None, "time_s": first.time_s, "parts": parts}
    rate = (parts["T_io_s"] * value["bw_big_MiBps"] + parts["temp_MiB"]) / parts["phase12_s"]   # MiB/s into the cache
    t_tail = K_TAIL * capacity / rate
    scale = 1.0 if t_tail >= expiry else 1 + (BURST_RATIO - 1) * min(1.0, (expiry - t_tail) / (expiry - M3_TAIL_S))
    p2 = params_with(base, page_cache_window_MiB=capacity, burst_s_per_GiB=value["burst_s_per_GiB"] * scale)
    with tail_limited_extents(t_tail):
        pred = predict_condition(cfg, p2, query, sf, settings)
    return {"capacity_MiB": capacity, "turnover_MiBps": rate, "t_tail_s": t_tail, "burst_scale": scale,
            "time_s": pred.time_s, "parts": pred.parts}

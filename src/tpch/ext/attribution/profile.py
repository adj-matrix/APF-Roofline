"""E3 (attribution): where a query's user cycles go, by component (docs/ext/attribution.md).

A profile is a system-wide `perf record -e cycles:u,instructions:u -c PERIOD` taken over one run. This module
reads it for the postgres processes only (the leader and its parallel workers), resolves the functions the
stripped binary does not export (static and LTO-local ones such as ExecInterpExpr and tts_heap_getsomeattrs)
from PostgreSQL's full symbol table, groups them into components, and reports per component the share of user
cycles, the share of user instructions and the IPC.

The symbol table comes from the PGDG debug package of the installed build (postgresql-18-dbgsym
18.6-1.pgdg22.04+2, build-id 11dd4ccc...). It is not installed system-wide; regenerate it with:
  curl -O http://apt.postgresql.org/pub/repos/apt/pool/main/p/postgresql-18/postgresql-18-dbgsym_18.6-1.pgdg22.04%2B2_amd64.deb
  dpkg-deb -x postgresql-18-dbgsym_*.deb x
  nm --defined-only x/usr/lib/debug/.build-id/11/dd4ccc*.debug | awk '$2 ~ /^[tTwW]$/ {print $1, $3}' | sort \
     > results/tools/postgres-18.6-11dd4ccc.syms

  PYTHONPATH=src .venv/bin/python -m tpch.ext.attribution.profile PERF_DATA [--out JSON]
"""
from __future__ import annotations

import argparse
import bisect
import collections
import json
import re
import subprocess
from pathlib import Path

SYMS = Path(__file__).resolve().parents[4] / "results" / "tools" / "postgres-18.6-11dd4ccc.syms"

# Components, first match wins (PostgreSQL 18.6 function names; LTO suffixes such as .lto_priv.0 are stripped).
COMPONENTS = [
    ("checksum", r"^(pg_checksum_|PageIsVerified|buffer_readv_complete|pgaio_)"),
    ("bufmgr", r"^(ReadBuffer|StartReadBuffer|WaitReadBuffers|PinBuffer|UnpinBuffer|ReleaseBuffer|BufTable|"
               r"StrategyGetBuffer|GetVictimBuffer|BufferAlloc|LockBuffer|LWLock|read_stream|BufferGetBlockNumber|"
               r"smgr|md(read|startreadv|nblocks|exists)|FileRead|ResourceOwner|hash_search_with_hash_value|"
               r"AsyncReadBuffers|PinBuffer_Locked|TerminateBufferIO|StartBufferIO|IncrBufferRefCount|"
               r"table_block_parallelscan|LockBufHdr|UnlockBufHdr|GetPrivateRefCount|ReservePrivateRefCount|"
               r"ForgetPrivateRefCount|NewPrivateRefCount|ConditionVariable|WaitIO|BufferIsPermanent)"),
    ("heap_page", r"^(heap_prepare_pagescan|heapgettup|heap_getnextslot|HeapTupleSatisfies|page_collect_tuples|"
                  r"heapgetpage|heap_page_prune|heap_scan_stream)"),
    ("deform", r"^(tts_|slot_|heap_deform|ExecStoreBufferHeapTuple|ExecStore|nocachegetattr|heap_getattr)"),
    ("expression", r"^(ExecInterpExpr|ExecEval|ExecJust|numeric|Numeric|cmp_var|cmp_abs|add_abs|sub_abs|add_var|"
                   r"sub_var|mul_var|div_var|make_result|init_var|alloc_var|free_var|set_var|zero_var|strip_var|"
                   r"round_var|trunc_var|apply_typmod|accum_sum|do_numeric|int8|int4|int2|date|float8|float4|"
                   r"timestamp|bool|pg_detoast|detoast|makeNumeric|make_var|numericvar|power_var|sqrt_var|"
                   r"get_str_from_var|in_range|DirectFunctionCall|FunctionCall|cmp_numerics)"),
    ("agg_exec", r"^(ExecAgg|agg_|advance_|lookup_hash|LookupTupleHash|TupleHash|BuildTupleHash|ExecScan|ExecProc|"
                 r"ExecGather|gather_|ExecSeqScan|SeqNext|Exec|finalize_|prepare_projection|hashagg|initialize_agg|"
                 r"MemoryContext|AllocSet|palloc|pfree|repalloc|Generation|shm_mq|tuplehash|"
                 r"standard_ExecutorRun|ExecutePlan|CHECK_FOR_INTERRUPTS|fetch_input_tuple|bpchar|hashbpchar|"
                 r"texteq|hashtext)"),
    ("hash_bytes", r"^hash_bytes"),           # shared: buffer tags (bufmgr) and group keys (agg); no call graph
]
COMPONENT_RE = [(name, re.compile(rx)) for name, rx in COMPONENTS]


class Symbols:
    def __init__(self, path: Path = SYMS):
        rows = [line.split() for line in path.read_text().splitlines() if line.strip()]
        self.addr = [int(a, 16) for a, _ in rows]
        self.name = [n for _, n in rows]

    def at(self, offset: int) -> str:
        i = bisect.bisect_right(self.addr, offset) - 1
        return self.name[i] if i >= 0 else f"0x{offset:x}"


def component(dso: str, sym: str) -> str:
    base = re.sub(r"\.(lto_priv|part|cold|isra|constprop)[.\d]*", "", sym)
    if dso.startswith("libc"):
        return "libc"
    if dso not in ("postgres",):
        return "other_dso"
    for name, rx in COMPONENT_RE:
        if rx.search(base):
            return name
    return "other"


def report(data: Path, comm: str = "postgres") -> str:
    """perf report text: samples per (dso, symbol) and event, for one comm (kept so components can be re-derived)."""
    return subprocess.run(["sudo", "-n", "perf", "report", "-f", "-i", str(data), "--stdio", "-n", "--comms", comm,
                          "--sort", "dso,sym", "--percent-limit", "0"], capture_output=True, text=True, check=True).stdout


def read(data: Path | None = None, comm: str = "postgres", text: str | None = None) -> dict:
    """{event: {(dso, symbol): samples}} for one comm, unresolved postgres addresses resolved."""
    out = text if text is not None else report(data, comm)
    syms = Symbols()
    events: dict = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"# Samples: .* of event '([^']+)'", line)
        if m:
            cur = events.setdefault(m.group(1), collections.Counter())
            continue
        if cur is None or line.startswith("#") or not line.strip():
            continue
        m = re.match(r"\s*[\d.]+%\s+(\d+)\s+(\S+)\s+\[[.k]\]\s+(.+?)\s*$", line)
        if not m:
            continue
        n, dso, sym = int(m.group(1)), m.group(2), m.group(3)
        if dso == "postgres" and sym.startswith("0x"):
            sym = syms.at(int(sym, 16))
        cur[(dso, sym)] += n
    return events


def summarize(events: dict) -> dict:
    cyc = events.get("cycles:u", collections.Counter())
    ins = events.get("instructions:u", collections.Counter())
    comp_c, comp_i = collections.Counter(), collections.Counter()
    for (dso, sym), n in cyc.items():
        comp_c[component(dso, sym)] += n
    for (dso, sym), n in ins.items():
        comp_i[component(dso, sym)] += n
    tc, ti = sum(comp_c.values()) or 1, sum(comp_i.values()) or 1
    comps = {k: {"cycles_share": comp_c[k] / tc, "instr_share": comp_i[k] / ti,
                 "ipc": comp_i[k] / comp_c[k] if comp_c[k] else None}
             for k in sorted(set(comp_c) | set(comp_i), key=lambda k: -comp_c[k])}
    top = [{"dso": d, "sym": s, "cycles_share": n / tc} for (d, s), n in cyc.most_common(40)]
    return {"samples_cycles": tc, "samples_instr": ti, "ipc": ti / tc, "components": comps, "top": top}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("data", type=Path, help="perf.data, or a saved report text (.txt)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--raw", type=Path, help="save the perf report text here")
    a = ap.parse_args()
    text = a.data.read_text() if a.data.suffix == ".txt" else report(a.data)
    if a.raw:
        a.raw.write_text(text)
    res = summarize(read(text=text))
    for k, v in res["components"].items():
        print(f"{k:11s} cycles {v['cycles_share'] * 100:5.1f} %  instr {v['instr_share'] * 100:5.1f} %  "
              f"IPC {v['ipc'] or 0:.2f}")
    print(f"IPC {res['ipc']:.2f}; top: " + ", ".join(f"{t['sym']} {t['cycles_share'] * 100:.1f}" for t in res["top"][:12]))
    if a.out:
        a.out.write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()

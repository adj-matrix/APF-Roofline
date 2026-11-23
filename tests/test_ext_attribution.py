"""E3 (tpch.ext.attribution): component rules, perf report parsing, symbol resolution, shares and IPC."""
from tpch.ext.attribution.profile import Symbols, component, read, summarize

REPORT = """\
# Samples: 1K of event 'cycles:u'
# Event count (approx.): 2000006000
#
# Overhead       Samples  Shared Object  Symbol
#
    40.00%           400  postgres       [.] 0x0000000000388800
    30.00%           300  postgres       [.] pg_checksum_page
    20.00%           200  postgres       [.] hash_search_with_hash_value
    10.00%           100  libc.so.6      [.] __memmove_avx_unaligned_erms


# Samples: 2K of event 'instructions:u'
#
    50.00%           900  postgres       [.] 0x0000000000388800
    30.00%           600  postgres       [.] pg_checksum_page
    10.00%           200  postgres       [.] hash_search_with_hash_value
    10.00%           100  libc.so.6      [.] __memmove_avx_unaligned_erms
"""


def test_component_rules():
    assert component("postgres", "ExecInterpExpr") == "expression"
    assert component("postgres", "tts_heap_getsomeattrs.lto_priv.0") == "deform"
    assert component("postgres", "pg_checksum_page") == "checksum"
    assert component("postgres", "hash_search_with_hash_value") == "bufmgr"
    assert component("postgres", "LockBufHdr") == "bufmgr"
    assert component("postgres", "cmp_numerics") == "expression"
    assert component("postgres", "heapgettup_pagemode") == "heap_page"
    assert component("postgres", "hashbpchar") == "agg_exec"
    assert component("postgres", "hash_bytes") == "hash_bytes"        # shared by bufmgr and agg: its own bucket
    assert component("libc.so.6", "memcpy") == "libc"
    assert component("[kernel.kallsyms]", "copy_user_generic_string") == "other_dso"
    assert component("postgres", "some_unknown_function") == "other"


def test_symbols_resolve_an_address_to_the_enclosing_function(tmp_path):
    f = tmp_path / "syms"
    f.write_text("0000000000388710 ExecInterpExpr\n000000000039d0a0 tts_heap_getsomeattrs.lto_priv.0\n")
    s = Symbols(f)
    assert s.at(0x388800) == "ExecInterpExpr"
    assert s.at(0x39d0d0) == "tts_heap_getsomeattrs.lto_priv.0"
    assert s.at(0x100) == "0x100"                                    # before the first symbol


def test_read_and_summarize(tmp_path):
    f = tmp_path / "syms"
    f.write_text("0000000000388710 ExecInterpExpr\n")
    events = read(text=REPORT, symbols=Symbols(f))
    assert events["cycles:u"][("postgres", "ExecInterpExpr")] == 400
    assert events["instructions:u"][("libc.so.6", "__memmove_avx_unaligned_erms")] == 100
    res = summarize(events)
    c = res["components"]
    assert res["samples_cycles"] == 1000 and res["samples_instr"] == 1800
    assert abs(c["expression"]["cycles_share"] - 0.4) < 1e-12
    assert abs(c["bufmgr"]["ipc"] - 1.0) < 1e-12                   # 200 instr samples over 200 cycle samples
    assert abs(c["expression"]["ipc"] - 900 / 400) < 1e-12
    assert abs(res["ipc"] - 1.8) < 1e-12
    assert res["top"][0]["sym"] == "ExecInterpExpr"


def test_read_without_addresses_needs_no_symbol_table():
    text = REPORT.replace("0x0000000000388800", "ExecInterpExpr")
    events = read(text=text)                                         # Symbols() is never loaded
    assert events["cycles:u"][("postgres", "ExecInterpExpr")] == 400

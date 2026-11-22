# Analytical Profiling Framework for Database Query Execution

This project extends the **Roofline model** into a performance model for database query execution. Roofline (Williams, Waterman and Patterson, 2009) bounds a program's time by two demands, work and data, against the machine's two roofs, compute and bandwidth. It is simple, and it explains *why* code runs at the speed it does. A database query breaks several of its assumptions:

- the work per tuple changes with the working set;
- I/O and computation do not always overlap;
- a spilling join is several kernels in sequence;
- the "compute roof" itself moves with how the CPU idles.

We kept Roofline's structure and extended it where the measurements demanded it. Each extension comes from the performance-modelling literature: the cache-aware Roofline, the main-memory database cost models, Little's law and operational analysis. Each was brought in by a prediction that failed.

The result predicts query times from the hardware up and attributes every second to a resource or a named mechanism. It is built to grow: asynchronous I/O and parallel workers have since been added the same way.

The model was developed on PostgreSQL 18 with TPC-H Q6, Q3 and Q1, from SF1 to SF1000 (1.5 TB, 25× the RAM), on a two-socket Xeon server with HDD RAID storage. TPC-H was the training ground: each scale step changed the bottleneck or the mechanism, and the model had to follow.

> A performance bug does not crash: the query runs and the number looks plausible. Telling a healthy system from a sick one needs an expected value first. A prediction from the hardware up, checked against the measurement, is an `assert` that spans software and hardware.

## From Roofline to a query

At every instant a backend is either on a CPU or off it, so a query's time is exactly

```
T = C / f + W        C: CPU cycles (the work)    f: the actual clock    W: time off the CPU
```

Counting work in cycles separates what the software does from how fast the hardware runs.

If reading and computing overlap perfectly, Roofline's bound follows:

```
T ≥ max(C / f, S / B)        S: bytes read    B: device bandwidth
```

Demand comes from the plan and the data. Capability comes from microbenchmarks.

## What a database breaks, and how the model was extended

| Roofline assumes | what the measurements showed | extension | lineage |
|---|---|---|---|
| compute and data movement overlap | Q6 at SF100: the device reports 100 % busy and delivers under 60 % of its bandwidth. Reading back spilled hash-join batches waits 4× longer per request than the table scan | a **gap term** G per phase, one named mechanism each. Little's law (outstanding requests = throughput × latency) says when the bound holds and what a wait costs. Read-back seeks = extents × 24.6 ms | Little 1961; Lazowska et al. 1984 (operational laws) |
| a constant cost per unit of work | Q3 warm, SF1 → SF10: +14.7 % cycles per tuple once the hash table (77 MiB) outgrows the 20 MiB L3. That is 1.06 extra misses per probe × 85 ns | **cache-aware demand**: the per-tuple cost is measured on a small-scale model and moved along a memory-hierarchy curve to the target, c(target) = c(SF10) + Δ(v_target) − Δ(v_SF10). It is never refitted | Ilic et al. 2014 (cache-aware Roofline); Boncz et al. 1999, Manegold et al. 2002 (database memory cost models) |
| cost linear in the data | SF1000 Q3: 15 % more user cycles than 10 × SF100 | **scale-variant variables**, each with its own mechanism and microbenchmark: partition fan-out (store-buffer stalls); BIGINT keys above SF 357; 32-bit hash "twin keys", whose false matches grow with SF² | Boncz et al. 1999 (radix-cluster fan-out); PostgreSQL `hashint8` |
| one kernel, one bound | a spilling hash join writes batch files, then reads them back | **phases** at the executor's blocking points: T = Σ_p ( max_r D_{p,r} / R_{p,r} + G_p ). A device shared by two streams in one phase is one demand, not two waits | Roofline per phase |
| a fixed compute roof | Q1 at SF100: −2.7 %, most of it in the clock. At 75 % on-CPU, short idles pin the clock at 1.2–1.5 GHz. Busy cores take the turbo bins (3.48 → 3.10 GHz) | **the clock as a roof that depends on the idle pattern**: on-CPU share, idle length, busy cores. The idle pattern comes from the phase's own gap, so clock and gap are solved together by fixed-point iteration | DVFS and governor behaviour, measured on this machine |
| one bandwidth number | the same RAID array streams 2.5 GB/s from one region, 1.3 GB/s to one buffered reader, and 620 MB/s to two parallel-scan readers leapfrogging 64 MiB chunks | **ceilings**: a device roof per access pattern (location, interleaving, request depth), each from a microbenchmark | Roofline ceilings |

Per phase p and resource r, the resulting form is

```
T_p = max_r ( D_{p,r} / R_{p,r} )  +  G_p
```

It is organised in three layers. Each layer has a rule about where its numbers may come from:

| layer | contents | values come from |
|---|---|---|
| **L1 roofs and ceilings** | bandwidth per access pattern; the clock as a function of the idle pattern | microbenchmarks only, never runs of the queries being predicted |
| **L2 demands** | counts (tuples, pages, batches, extents, false matches) × cost per unit | the plan, the catalog, the same query at small scale, purpose-built database variants |
| **L3 gaps** | start-up, synchronous waits, write-back bursts, read-back seeks | microbenchmarks that reproduce one mechanism and sweep its variable |

A few rules keep the model honest:

- A parameter fitted on query runs is marked provisional and must name its mechanism.
- Every test of a new scale or configuration is registered before it runs: the prediction, the parameter digest and the code hash.
- A miss is fixed in the layer it points to; the total is never rescaled.
- A change is accepted only if it improves the individual terms. Improving T through compensating errors does not count.

## What it predicts

**Base model** (one backend, synchronous I/O). `./tpch model evaluate` replays all 21 registered conditions:

| conditions | time error |
|---|---|
| 15 training and test conditions at SF100 (Q6, Q1, Q3 at five `work_mem` settings) | within ±2.1 % |
| 6 blind tests registered before running: Q6, Q1, Q3 × SF1000 (Q3 at three `work_mem` settings), Q3 × SF100 at an unseen `work_mem` | within ±2.8 % |

CPU cycles are within ±2.3 % in every condition. The error that remains is in the clock and the time off the CPU.

**Extensions** use the same method: mechanism, then microbenchmark, then a prediction registered before the run.

| extension | what it adds | status |
|---|---|---|
| E1 asynchronous I/O | `io_method` (sync / worker / io_uring) × direct I/O: where the page-cache copy and the page verification run, and what that does to the clock | 10 cells and 4 held-out conditions within ±4 % |
| E2 parallel workers | the scan past the Roofline ridge: the device's ceiling under interleaved streams, and the clock with busy cores | sync, io_uring and direct I/O: 25 of 28 cells within ±5 %. The device's rate is set by the access pattern, not by requests in flight: a parallel scan's leapfrog over 64 MiB chunks caps two participants at ~620 MB/s. With worker-mode I/O a run switches between that rate and ~1 GB/s, so it is predicted as a range. Open: 8 participants run ~6 % slow (hyperthread siblings suspected) |
| E3 attribution | the per-tuple user cycles opened up into buffer manager, executor and expressions, as workers are added | Working |
| E4 page-cache simulation | the one remaining count fitted on query runs (how much of a spill the page cache keeps), replaced by a simulation of Linux's page cache | Working |

## Why a model and not a benchmark

A benchmark reports a number. The model also says how far that number is from what the hardware allows, and why. Some uses so far:

- **Expected values as assertions.** A cold run finished in 1.7 s against 4.7 s predicted. The data turned out to be still in the RAID controller's write-back cache, which `drop_caches` does not touch. Every cold run now flushes it first.
- **Bottleneck flips, predicted.** Q6 was predicted to turn CPU-bound on the faster array at SF100, at 143 s. It took 142.8 s.
- **Configurations that hurt.** One parallel worker was predicted to make Q6 *slower* than a single backend (155 s vs 142 s). Two participants reading leapfrogging 64 MiB chunks drop the array from ~1 GB/s to ~600 MB/s. It took 153.5 s.
- **Mechanisms a linear model cannot see.** `hashint8` folds a 64-bit key into 32 bits, so every key ≥ 2³² has a twin. Together with chance collisions, false hash matches grow with SF². This was calibrated at SF10 on a database built to contain twins, then predicted SF1000 blind.

## Where it goes next

- The machine side is portable by construction. On another machine, re-running the L1 and L3 microbenchmarks (`hardware/`, `src/tpch/bench/`) is enough. NVMe storage and newer CPUs would mainly change the ceilings and the clock model.
- The workload side extends operator by operator. Scans, aggregation and spilling hash joins are modelled. Sorts, index access and concurrent queries are the natural next demands and gaps.

## Repository

```
src/tpch/           the framework (Python package, CLI ./tpch)
  model/                     the base model: calibrate, predict, evaluate
  ext/                       extensions E1-E4 (read-only use of the base model)
  runner.py, experiment.py   measured runs: randomized block designs, cold / warm cache, per-run samplers
  monitor.py, counters.py    iostat / mpstat / pidstat / perf; exact kernel counters; procmon.py: CPU by process
  sysconf.py, cache.py       system state during measurements, applied at the start and restored at the end
  bench/                     microbenchmarks: device bandwidth vs location, memory latency
hardware/           machine characterisation: clock vs idle pattern, controller cache, write-back, read-back
queries/            per query (q1, q3, q6) and per cell (sfN/): SQL, small-scale variants, run scripts
model/              parameters (params.json, each value with the data it came from), predictions, evaluations
src/setup/          reproducible environment: PostgreSQL 18 from PGDG, dbgen at a pinned commit, cluster config
tests/              unit tests on recorded sampler output
```

Typical use:

```
src/setup/setup.sh                                  # packages, PostgreSQL 18, dbgen, cluster, data
./tpch model predict --query q6 --sf 100            # prediction from EXPLAIN only; nothing is executed
./tpch experiment --query q6 --sf 100 --cache cold --blocks 3
./tpch model evaluate                               # replay every registered condition, term by term
```

Long experiments run as systemd units (`src/setup/detach.sh NAME command…`), so they survive the session.

## Environment

The reported numbers come from 2 × Intel Xeon E5-2680 (Sandy Bridge-EP, 8 cores and 20 MiB L3 each) with 62 GB of RAM, and HDD storage on a PERC H710P: one volume at ~188 MB/s (SF1–SF10) and one at ~1 GB/s (SF100–SF1000). The software is Linux 5.15 and PostgreSQL 18.6.

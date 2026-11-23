"""E4: read files sequentially at a paced rate (MiB/s) for a duration, through the page cache: the table scan that
turns a NUMA node's page cache over while a spilling join writes its batch files (docs/ext/cachesim.md, M3).
  python -m tpch.ext.cachesim.reader LIST RATE_MIBPS DUR_S        (LIST: one path per line; read-only)
"""
import sys
import time

CHUNK = 1 << 20


def main() -> None:
    files = [line.strip() for line in open(sys.argv[1]) if line.strip()]
    rate, dur = float(sys.argv[2]), float(sys.argv[3])
    t0 = time.monotonic()
    done = 0
    for path in files:
        with open(path, "rb", buffering=0) as f:
            while True:
                if time.monotonic() - t0 >= dur:
                    print(f"read {done / CHUNK:.0f} MiB in {time.monotonic() - t0:.1f} s", flush=True)
                    return
                if not f.read(CHUNK):
                    break
                done += CHUNK
                ahead = done / CHUNK / rate - (time.monotonic() - t0)
                if ahead > 0:
                    time.sleep(ahead)
    print(f"read {done / CHUNK:.0f} MiB in {time.monotonic() - t0:.1f} s (end of list)", flush=True)


if __name__ == "__main__":
    main()

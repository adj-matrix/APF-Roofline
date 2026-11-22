"""Moved to tpch.procmon (shared infrastructure since E2); this name stays for E1's scripts."""
import sys

from ...procmon import category, main, run, sample, window  # noqa: F401

if __name__ == "__main__":
    sys.exit(main())

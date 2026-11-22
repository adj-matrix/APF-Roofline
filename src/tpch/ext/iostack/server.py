"""Moved to tpch.pgserver (shared infrastructure since E2); this name stays for E1's scripts."""
import sys

from ...pgserver import main, show  # noqa: F401

if __name__ == "__main__":
    main(sys.argv[1:])

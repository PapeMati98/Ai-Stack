"""Portable stdlib CLI entry point; no live mode."""
from dots_farm.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

"""Entry point: ``python -m pms <command>`` (see :mod:`pms.cli`)."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

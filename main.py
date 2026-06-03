#!/usr/bin/env python3
"""CLI entry point: `python main.py VUSXX`.

Thin shim around prospectus_fetcher.cli so the package stays importable/testable.
"""

import sys

from prospectus_fetcher.cli import main

if __name__ == "__main__":
    sys.exit(main())

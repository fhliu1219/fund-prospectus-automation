"""Enable `python -m prospectus_fetcher VUSXX`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

"""`python -m skillgram <command>`: the same entry point as the console script.

It exists so a harness can be pointed at the tool without knowing where it was
installed (`launch_argv` falls back to this when there is no checkout shim).
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

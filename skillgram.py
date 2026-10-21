#!/usr/bin/env python3
"""`python skillgram.py <command>` from a checkout: a shim, not the implementation.

The tool lives in the `skillgram/` package. This file exists so the documented
`python skillgram.py install` (and the CI smoke step) keep working without an
install, and so the MCP launcher can point at a path that imports cleanly.
"""
import sys

from skillgram import main

if __name__ == "__main__":
    sys.exit(main())

"""Compatibility import for the composed FastAPI application."""

import sys

from nico_app import main as _main

sys.modules[__name__] = _main

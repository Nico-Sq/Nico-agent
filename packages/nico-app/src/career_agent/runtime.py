"""Compatibility import for the application runtime."""

import sys

from nico_app import runtime as _runtime

sys.modules[__name__] = _runtime

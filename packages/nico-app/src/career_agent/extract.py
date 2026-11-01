"""Compatibility import for material text extraction."""

import sys

from nico_adapters import extract as _extract

sys.modules[__name__] = _extract

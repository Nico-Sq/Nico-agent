"""Compatibility import for prompt and message assembly."""

import sys

from nico_application import context as _context

sys.modules[__name__] = _context

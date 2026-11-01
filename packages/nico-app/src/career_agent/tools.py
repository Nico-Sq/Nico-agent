"""Compatibility module alias for application-owned tool definitions."""

import sys

from nico_application import tools as _tools

sys.modules[__name__] = _tools

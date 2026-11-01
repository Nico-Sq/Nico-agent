"""Compatibility module alias for trusted local tool plugins."""

import sys

from nico_adapters import plugins as _plugins

sys.modules[__name__] = _plugins

"""Compatibility module alias for the extracted storage repository."""

import sys

from nico_storage import store as _store

sys.modules[__name__] = _store

"""Compatibility module alias for storage-backed authentication."""

import sys

from nico_storage import auth as _auth

sys.modules[__name__] = _auth

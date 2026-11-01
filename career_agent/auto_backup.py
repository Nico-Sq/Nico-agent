"""Compatibility module alias for automatic business database backups."""

import sys

from nico_storage import backup as _backup

sys.modules[__name__] = _backup

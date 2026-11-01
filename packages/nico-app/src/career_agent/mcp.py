"""Compatibility module alias for the local stdio MCP adapter."""

import sys

from nico_adapters import mcp as _mcp

sys.modules[__name__] = _mcp

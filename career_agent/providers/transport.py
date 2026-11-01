"""Compatibility import for the shared model HTTP transport."""

import asyncio

import httpx

from nico_adapters.models.transport import post_json

__all__ = ["asyncio", "httpx", "post_json"]

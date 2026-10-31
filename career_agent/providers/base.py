"""The adapter contract shared by every model protocol.

An adapter only translates between the runtime's message list and one wire
protocol; retries, headers and error codes stay in the transport layer.
"""

from typing import Protocol


class Adapter(Protocol):
    def request(self, model: str, messages: list[dict], tools: list[dict],
                max_tokens: int) -> tuple[str, dict, dict]:
        """Return ``(endpoint, extra_headers, payload)`` for one completion request."""
        ...

    def parse(self, data: dict) -> dict:
        """Normalize a provider response into the runtime's reply shape."""
        ...

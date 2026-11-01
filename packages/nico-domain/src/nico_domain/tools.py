"""Tool permission rules that do not depend on adapters or storage."""


def tool_is_read_only(read_only_hint: object) -> bool:
    """Only an explicit boolean true marks an external tool read-only."""
    return read_only_hint is True

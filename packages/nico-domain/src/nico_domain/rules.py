"""Validation rules that do not depend on storage or delivery frameworks."""


def required_text(value: str, empty_message: str = "内容不能为空") -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(empty_message)
    return normalized


def is_pending_memory_candidate(status: str) -> bool:
    return status == "pending"


def can_accept_chat_message(status: str) -> bool:
    return status in {"active", "running"}


def is_current_chat_run(status: str, current_version: int, expected_version: int) -> bool:
    return status == "running" and current_version == expected_version


def can_create_claim_from_material(kind: str, revoked_at: str | None) -> bool:
    return kind in {"resume", "project"} and revoked_at is None

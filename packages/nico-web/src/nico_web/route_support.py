"""Shared HTTP helpers for routes backed by application use cases."""

from fastapi import HTTPException

from nico_application import SessionUseCases, UseCaseError

from .backend import active_store


def http_use_case_error(exc: UseCaseError) -> HTTPException:
    status = {"not_found": 404, "conflict": 409, "invalid": 422}[exc.kind]
    return HTTPException(status, str(exc))


def http_interview_use_case_error(exc: UseCaseError) -> HTTPException:
    if exc.kind == "not_found" and str(exc) == "面试会话不存在":
        return HTTPException(404, "会话不存在")
    if exc.kind == "invalid" and str(exc) == "回答不能为空":
        return HTTPException(422, "内容不能为空")
    return http_use_case_error(exc)


def active_session_use_cases():
    return SessionUseCases(active_store())


def require_session(session_id: str, kind: str) -> dict:
    try:
        return active_session_use_cases().require_session(session_id, kind)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc

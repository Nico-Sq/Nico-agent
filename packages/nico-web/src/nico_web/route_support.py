"""Shared HTTP helpers for routes backed by application use cases."""

from fastapi import HTTPException

from nico_application import UseCaseError

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


def require_session(session_id: str, kind: str) -> dict:
    session = active_store().get(session_id, kind)
    if session is None:
        raise HTTPException(404, "会话不存在")
    return session

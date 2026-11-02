"""Memory and memory-candidate HTTP routes and request models."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from nico_application import MemoryUseCases, UseCaseError

from .backend import active_store
from .route_support import http_use_case_error, require_session


router = APIRouter()


def active_memory_use_cases():
    return MemoryUseCases(active_store())


class LongMemoryInput(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class MemoryCandidateInput(LongMemoryInput):
    source_message_ids: list[int] = Field(min_length=1, max_length=16)


class MemoryCandidateEdit(LongMemoryInput):
    pass


@router.get("/api/memories")
def list_memories(q: str = Query(default="", max_length=100)):
    return active_memory_use_cases().search(q)


@router.post("/api/memories")
def add_memory(body: LongMemoryInput):
    try:
        return active_memory_use_cases().add(body.content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: str):
    try:
        active_memory_use_cases().delete(memory_id)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    return {"deleted": True}


@router.get("/api/memory-candidates")
def list_memory_candidates(
    session_id: str | None = Query(default=None),
    status: Literal["pending", "confirmed"] | None = Query(default=None),
):
    if session_id is not None:
        require_session(session_id, "chat")
    try:
        return active_memory_use_cases().list_candidates(session_id, status)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc


@router.get("/api/memory-candidates/{candidate_id}")
def get_memory_candidate(candidate_id: str):
    try:
        return active_memory_use_cases().get_candidate(candidate_id)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc


@router.post("/api/chat/sessions/{session_id}/memory-candidates")
def create_memory_candidate(session_id: str, body: MemoryCandidateInput):
    require_session(session_id, "chat")
    try:
        return active_memory_use_cases().create_candidate(
            session_id, body.content, body.source_message_ids
        )
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/api/memory-candidates/{candidate_id}")
def update_memory_candidate(candidate_id: str, body: MemoryCandidateEdit):
    try:
        return active_memory_use_cases().edit_candidate(candidate_id, body.content)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/memory-candidates/{candidate_id}/confirm")
def confirm_memory_candidate(candidate_id: str):
    try:
        return active_memory_use_cases().confirm_candidate(candidate_id)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/api/memory-candidates/{candidate_id}")
def delete_memory_candidate(candidate_id: str):
    try:
        active_memory_use_cases().delete_candidate(candidate_id)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    return {"deleted": True}

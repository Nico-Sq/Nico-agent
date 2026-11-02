"""Chat HTTP routes and request models."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from nico_application import UseCaseError

from .route_support import http_use_case_error
from .workflow_support import (
    ChatRunInput,
    TextInput,
    active_chat_use_cases,
    complete_chat_request,
    stream_response,
    wants_sse,
)


router = APIRouter()


class ChatCreateInput(BaseModel):
    model_id: str = Field(default="default", min_length=1, max_length=80)


class ChatMemoryInput(BaseModel):
    text: str = Field(max_length=2000)


@router.post("/api/chat/sessions")
def create_chat(body: ChatCreateInput | None = None):
    model_id = body.model_id if body else "default"
    try:
        return active_chat_use_cases().create(model_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/api/chat/sessions/{session_id}/memory")
def update_chat_memory(session_id: str, body: ChatMemoryInput):
    try:
        return {"text": active_chat_use_cases().set_memory(session_id, body.text)}
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc


@router.post("/api/chat/sessions/{session_id}/messages")
async def chat_message(session_id: str, body: TextInput, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_chat_use_cases().send_message(
            session_id, body.text, on_event,
        ))
    try:
        return await complete_chat_request(
            active_chat_use_cases().send_message(session_id, body.text)
        )
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/chat/sessions/{session_id}/messages/claim")
def claim_chat_message(session_id: str, body: TextInput):
    try:
        return active_chat_use_cases().claim_message(session_id, body.text)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/chat/sessions/{session_id}/messages/run")
async def run_chat_message(session_id: str, body: ChatRunInput, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_chat_use_cases().run_claimed_message(
            session_id, body.version, on_event,
        ))
    try:
        return await complete_chat_request(
            active_chat_use_cases().run_claimed_message(session_id, body.version)
        )
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc


@router.post("/api/chat/sessions/{session_id}/retry")
async def retry_chat_message(session_id: str, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_chat_use_cases().retry_message(
            session_id, on_event,
        ))
    try:
        return await complete_chat_request(
            active_chat_use_cases().retry_message(session_id)
        )
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc


@router.post("/api/chat/sessions/{session_id}/interrupt")
def interrupt_chat(session_id: str):
    try:
        return active_chat_use_cases().interrupt(session_id)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc

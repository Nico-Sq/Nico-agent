"""Interview HTTP routes and request models."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from nico_application import UseCaseError

from .route_support import http_interview_use_case_error
from .workflow_support import (
    ChatRunInput,
    TextInput,
    active_interview_use_cases,
    complete_interview_request,
    stream_response,
    wants_sse,
)


router = APIRouter()


class InterviewInput(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    max_questions: int = Field(default=3, ge=1, le=10)
    material_ids: list[str] = Field(default_factory=list, max_length=5)
    claim_ids: list[str] = Field(default_factory=list, max_length=10)
    model_id: str = Field(default="default", min_length=1, max_length=80)


class InterviewAnswerInput(TextInput):
    question_number: int = Field(ge=1)


class PracticeInput(BaseModel):
    question_number: int = Field(ge=1)


@router.post("/api/interview/sessions")
def create_interview(body: InterviewInput):
    try:
        return active_interview_use_cases().create(
            body.goal, tuple(body.material_ids), tuple(body.claim_ids),
            body.max_questions, body.model_id,
        )
    except ValueError as exc:
        message = "内容不能为空" if str(exc) == "岗位目标不能为空" else str(exc)
        raise HTTPException(422, message) from exc


@router.get("/api/interview/sessions/{session_id}/report")
def interview_report(session_id: str):
    try:
        return active_interview_use_cases().report(session_id)
    except UseCaseError as exc:
        raise http_interview_use_case_error(exc) from exc


@router.post("/api/interview/sessions/{session_id}/practice")
def create_practice(session_id: str, body: PracticeInput):
    try:
        return active_interview_use_cases().create_practice(session_id, body.question_number)
    except UseCaseError as exc:
        raise http_interview_use_case_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/interview/sessions/{session_id}/start")
async def start_interview(session_id: str, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_interview_use_cases().start(
            session_id, on_event,
        ))
    return await complete_interview_request(active_interview_use_cases().start(session_id))


@router.post("/api/interview/sessions/{session_id}/answers")
async def answer_interview(session_id: str, body: TextInput, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_interview_use_cases().answer(
            session_id, body.text, on_event,
        ))
    return await complete_interview_request(active_interview_use_cases().answer(session_id, body.text))


@router.post("/api/interview/sessions/{session_id}/answers/claim")
def claim_interview_answer(session_id: str, body: InterviewAnswerInput):
    try:
        return active_interview_use_cases().claim_answer(
            session_id, body.text, body.question_number,
        )
    except UseCaseError as exc:
        raise http_interview_use_case_error(exc) from exc


@router.post("/api/interview/sessions/{session_id}/answers/run")
async def run_interview_answer(session_id: str, body: ChatRunInput, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_interview_use_cases().run_claimed_answer(
            session_id, body.version, on_event,
        ))
    return await complete_interview_request(
        active_interview_use_cases().run_claimed_answer(session_id, body.version)
    )


@router.post("/api/interview/sessions/{session_id}/skip")
async def skip_interview_question(session_id: str, body: PracticeInput, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_interview_use_cases().skip(
            session_id, body.question_number, on_event,
        ))
    return await complete_interview_request(
        active_interview_use_cases().skip(session_id, body.question_number)
    )


@router.post("/api/interview/sessions/{session_id}/pause")
def pause_interview(session_id: str):
    try:
        return active_interview_use_cases().pause(session_id)
    except UseCaseError as exc:
        raise http_interview_use_case_error(exc) from exc


@router.post("/api/interview/sessions/{session_id}/cancel")
def cancel_interview(session_id: str):
    try:
        return active_interview_use_cases().cancel(session_id)
    except UseCaseError as exc:
        raise http_interview_use_case_error(exc) from exc


@router.post("/api/interview/sessions/{session_id}/resume")
async def resume_interview(session_id: str, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_interview_use_cases().resume(
            session_id, on_event,
        ))
    return await complete_interview_request(active_interview_use_cases().resume(session_id))

"""Shared HTTP helpers for chat and interview runs."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from nico_application import ChatUseCases, InterviewUseCases, UseCaseError
from nico_contracts.errors import ModelError
from nico_contracts.events import RunEventSink

from .backend import active_backend, active_runtime as _active_runtime, active_store as _active_store
from .route_support import http_interview_use_case_error


logger = logging.getLogger("nico_web.routes")


class TextInput(BaseModel):
    text: str = Field(min_length=1, max_length=12000)


class ChatRunInput(BaseModel):
    version: int = Field(ge=1)


def active_store():
    return _active_store()


def active_runtime():
    return _active_runtime()


def active_chat_use_cases():
    backend = active_backend()
    return ChatUseCases(active_store(), backend.model_catalog(), run_chat_session,
                        backend.cancel_session_runs)


def active_interview_use_cases():
    backend = active_backend()
    return InterviewUseCases(active_store(), backend.model_catalog(), run_interview_session,
                             backend.cancel_session_runs)


async def run_chat_session(session: dict, on_event: RunEventSink | None = None) -> dict:
    backend = active_backend()
    selected = session.get("model_id", "default")
    run = active_runtime() if selected == "default" else backend.make_runtime(
        active_store(), backend.model_catalog().get(selected),
    )
    return await run.run(session, on_event=on_event)


async def run_interview_session(session: dict, result_handler,
                                on_event: RunEventSink | None = None) -> dict:
    backend = active_backend()
    selected = session.get("model_id", "default")
    run = active_runtime() if selected == "default" else backend.make_runtime(
        active_store(), backend.model_catalog().get(selected),
    )
    return await run.run(session, result_handler=result_handler, on_event=on_event)


def wants_sse(request: Request) -> bool:
    return "text/event-stream" in request.headers.get("accept", "").lower()


def stream_response(operation: Callable[[RunEventSink], Awaitable[dict]]) -> StreamingResponse:
    async def events():
        queue = asyncio.Queue()
        finished = object()

        async def run():
            try:
                await operation(queue.put_nowait)
                queue.put_nowait({"type": "done"})
            except UseCaseError as exc:
                status = {"not_found": 404, "conflict": 409, "invalid": 422}[exc.kind]
                queue.put_nowait({"type": "error", "detail": str(exc), "code": "request_rejected",
                                  "status": status})
            except ModelError as exc:
                logger.warning("Model run failed: code=%s", exc.code)
                queue.put_nowait({"type": "error", "detail": str(exc), "code": exc.code,
                                  "status": exc.status_code})
            except ValueError:
                logger.warning("Model run failed: code=model_invalid_output")
                queue.put_nowait({"type": "error", "detail": "模型输出或上下文不符合要求",
                                  "code": "model_invalid_output", "status": 502})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Model run failed: code=model_run_failed")
                queue.put_nowait({"type": "error", "detail": "模型运行失败，请稍后重试",
                                  "code": "model_run_failed", "status": 502})
            finally:
                queue.put_nowait(finished)

        task = asyncio.create_task(run())
        try:
            yield "data: {\"type\":\"started\"}\n\n"
            while True:
                event = await queue.get()
                if event is finished:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def complete_chat_request(result) -> dict | JSONResponse:
    try:
        return await result
    except UseCaseError:
        raise
    except ModelError as exc:
        logger.warning("Model run failed: code=%s", exc.code)
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status_code)
    except ValueError:
        logger.warning("Model run failed: code=model_invalid_output")
        return JSONResponse({"detail": "模型输出或上下文不符合要求", "code": "model_invalid_output"}, status_code=502)
    except Exception:
        logger.error("Model run failed: code=model_run_failed")
        return JSONResponse({"detail": "模型运行失败，请稍后重试", "code": "model_run_failed"}, status_code=502)


async def complete_interview_request(result) -> dict | JSONResponse:
    try:
        return await complete_chat_request(result)
    except UseCaseError as exc:
        raise http_interview_use_case_error(exc) from exc

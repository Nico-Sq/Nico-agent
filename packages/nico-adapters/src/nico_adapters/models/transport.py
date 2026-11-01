"""HTTP JSON transport shared by every adapter: one retry policy, one error map."""

import asyncio
import logging
import math

from nico_contracts.errors import ModelError

logger = logging.getLogger(__name__)
MAX_RETRY_AFTER_SECONDS = 30


def retry_delay(response) -> float | None:
    """Use a reasonable numeric Retry-After; skip retries that outlast one run."""
    value = response.headers.get("retry-after")
    if value is None:
        return 1
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 1
    if not math.isfinite(seconds) or seconds < 0:
        return 1
    return seconds if seconds <= MAX_RETRY_AFTER_SECONDS else None


def log_upstream_error(response) -> None:
    request_id = response.headers.get("x-request-id", "")
    request_id = "".join(
        char for char in request_id[:80] if char.isalnum() or char in "-_.:"
    ) or "-"
    raw_retry_after = response.headers.get("retry-after")
    try:
        retry_after = float(raw_retry_after) if raw_retry_after is not None else None
    except (TypeError, ValueError):
        retry_after = None
    if retry_after is not None and not math.isfinite(retry_after):
        retry_after = None
    logger.warning(
        "Model upstream returned HTTP %s (request_id=%s, retry_after=%s)",
        response.status_code, request_id,
        str(retry_after) if retry_after is not None else "-",
    )


async def post_json(base_url: str, endpoint: str, headers: dict, payload: dict) -> dict:
    """POST a JSON payload and return the decoded body, mapping failures to ModelError."""
    import httpx

    async with httpx.AsyncClient(timeout=60) as client:
        for attempt in range(2):
            try:
                response = await client.post(
                    f"{base_url.rstrip('/')}/{endpoint}", headers=headers, json=payload
                )
                response.raise_for_status()
                try:
                    data = response.json()
                except ValueError as exc:
                    raise ModelError("model_invalid_response", "模型服务返回了无效响应") from exc
                if not isinstance(data, dict):
                    raise ModelError("model_invalid_response", "模型服务返回了无效响应")
                return data
            except httpx.TimeoutException as exc:
                raise ModelError("model_timeout", "模型请求超时", 504) from exc
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if attempt == 0 and status in (429, 503):
                    delay = retry_delay(exc.response)
                    if delay is not None:
                        log_upstream_error(exc.response)
                        await asyncio.sleep(delay)
                        continue
                log_upstream_error(exc.response)
                if status in (401, 403):
                    raise ModelError("model_auth_failed", "模型服务认证失败，请检查配置") from exc
                if status in (400, 404, 422):
                    raise ModelError("model_request_rejected", "模型接口或参数不兼容，请检查配置") from exc
                if status in (429, 503):
                    raise ModelError("model_busy", "模型服务繁忙，请稍后重试", 503) from exc
                raise ModelError("model_unavailable", "模型服务暂时不可用") from exc
            except httpx.ConnectError as exc:
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                raise ModelError("model_unavailable", "无法连接模型服务") from exc
            except httpx.RequestError as exc:
                raise ModelError("model_unavailable", "无法连接模型服务") from exc
    raise ModelError("model_unavailable", "模型服务暂时不可用")


async def post_sse(base_url: str, endpoint: str, headers: dict, payload: dict):
    """Yield data fields from a Server-Sent Events response."""
    import httpx

    async with httpx.AsyncClient(timeout=60) as client:
        for attempt in range(2):
            try:
                async with client.stream(
                    "POST", f"{base_url.rstrip('/')}/{endpoint}",
                    headers=headers, json=payload,
                ) as response:
                    if attempt == 0 and response.status_code in (429, 503):
                        await response.aread()
                        delay = retry_delay(response)
                        if delay is not None:
                            log_upstream_error(response)
                            await asyncio.sleep(delay)
                            continue
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            yield line[5:].strip()
                    return
            except httpx.TimeoutException as exc:
                raise ModelError("model_timeout", "模型请求超时", 504) from exc
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                log_upstream_error(exc.response)
                if status in (401, 403):
                    raise ModelError("model_auth_failed", "模型服务认证失败，请检查配置") from exc
                if status in (400, 404, 422):
                    raise ModelError("model_request_rejected", "模型接口或参数不兼容，请检查配置") from exc
                if status in (429, 503):
                    raise ModelError("model_busy", "模型服务繁忙，请稍后重试", 503) from exc
                raise ModelError("model_unavailable", "模型服务暂时不可用") from exc
            except httpx.ConnectError as exc:
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                raise ModelError("model_unavailable", "无法连接模型服务") from exc
            except httpx.RequestError as exc:
                raise ModelError("model_unavailable", "无法连接模型服务") from exc
    raise ModelError("model_unavailable", "模型服务暂时不可用")

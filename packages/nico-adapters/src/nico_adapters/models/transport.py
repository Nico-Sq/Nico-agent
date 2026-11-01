"""HTTP JSON transport shared by every adapter: one retry policy, one error map."""

import asyncio

from nico_contracts.errors import ModelError


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
                    await asyncio.sleep(1)
                    continue
                if status in (401, 403):
                    raise ModelError("model_auth_failed", "模型服务认证失败，请检查配置") from exc
                if status in (400, 404, 422):
                    raise ModelError("model_request_rejected", "模型接口或参数不兼容，请检查配置") from exc
                if status in (429, 503):
                    raise ModelError("model_busy", "模型服务繁忙，请稍后重试", 503) from exc
                raise ModelError("model_unavailable", "模型服务暂时不可用") from exc
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
                        await asyncio.sleep(1)
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
                if status in (401, 403):
                    raise ModelError("model_auth_failed", "模型服务认证失败，请检查配置") from exc
                if status in (400, 404, 422):
                    raise ModelError("model_request_rejected", "模型接口或参数不兼容，请检查配置") from exc
                if status in (429, 503):
                    raise ModelError("model_busy", "模型服务繁忙，请稍后重试", 503) from exc
                raise ModelError("model_unavailable", "模型服务暂时不可用") from exc
            except httpx.RequestError as exc:
                raise ModelError("model_unavailable", "无法连接模型服务") from exc
    raise ModelError("model_unavailable", "模型服务暂时不可用")

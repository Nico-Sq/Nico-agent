import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from career_agent.models import ModelCatalog
from career_agent.provider import Provider
from career_agent.providers import ModelError


class FakeClient:
    """Stand-in for httpx.AsyncClient; each outcome is (status, body) or an exception."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    async def post(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers or {}, "payload": json})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        status, body = outcome
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


def openai_body(content="ok"):
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}


class ProviderAdapterTests(unittest.TestCase):
    def complete(self, provider, client, messages=None, tools=None):
        with patch("career_agent.providers.transport.httpx.AsyncClient", return_value=client):
            return asyncio.run(provider.complete(
                messages or [{"role": "user", "content": "问题"}], tools or []))

    def test_openai_request_and_reply(self):
        client = FakeClient((200, openai_body("hi")))
        provider = Provider(mode="openai", model="gpt-test", base_url="https://example.test/v1",
                            api_key="secret", max_output=4096)
        reply = self.complete(provider, client, tools=[{"type": "function", "function": {
            "name": "read_material", "description": "d", "parameters": {"type": "object"}}}])

        request = client.requests[0]
        self.assertEqual(request["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(request["payload"]["max_tokens"], 4096)
        self.assertEqual(request["payload"]["tools"][0]["function"]["name"], "read_material")
        self.assertEqual(reply["content"], "hi")
        self.assertEqual(reply["finish_reason"], "stop")
        self.assertEqual(reply["usage"]["total_tokens"], 7)

    def test_openai_compatible_omits_empty_tools(self):
        client = FakeClient((200, openai_body()))
        provider = Provider(mode="openai_compatible", model="local", base_url="http://127.0.0.1:1234/v1")
        self.complete(provider, client)
        self.assertNotIn("tools", client.requests[0]["payload"])
        self.assertNotIn("Authorization", client.requests[0]["headers"])

    def test_cancelling_provider_request_closes_transport(self):
        class BlockingClient:
            def __init__(self):
                self.started = asyncio.Event()
                self.cancelled = False
                self.closed = False

            async def post(self, *_args, **_kwargs):
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                self.closed = True

        async def scenario():
            client = BlockingClient()
            provider = Provider(mode="openai", model="m", base_url="https://example.test/v1",
                                api_key="k")
            with patch("career_agent.providers.transport.httpx.AsyncClient", return_value=client):
                task = asyncio.create_task(provider.complete([{"role": "user", "content": "问题"}], []))
                await client.started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            return client

        client = asyncio.run(scenario())
        self.assertTrue(client.cancelled)
        self.assertTrue(client.closed)

    def test_anthropic_request_and_reply(self):
        body = {"content": [{"type": "text", "text": "结论"},
                            {"type": "tool_use", "id": "call-1", "name": "read_material", "input": {"id": "m1"}}],
                "stop_reason": "tool_use", "usage": {"input_tokens": 11, "output_tokens": 4}}
        client = FakeClient((200, body))
        provider = Provider(mode="anthropic", model="claude-test", base_url="https://api.anthropic.test/v1",
                            api_key="secret")
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "看看材料"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "function": {
                "name": "read_material", "arguments": json.dumps({"id": "m1"})}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
        ]
        reply = self.complete(provider, client, messages=messages)

        request = client.requests[0]
        self.assertEqual(request["url"], "https://api.anthropic.test/v1/messages")
        self.assertEqual(request["headers"]["anthropic-version"], "2026-10-21")
        self.assertEqual(request["payload"]["system"], "系统提示")
        self.assertEqual(request["payload"]["messages"][1]["content"][0]["type"], "tool_use")
        self.assertEqual(reply["finish_reason"], "tool_calls")
        self.assertEqual(reply["tool_calls"][0]["function"]["name"], "read_material")
        self.assertEqual(reply["usage"]["total_tokens"], 15)

    def test_transient_status_is_retried_once(self):
        client = FakeClient((429, {"error": "busy"}), (200, openai_body("retried")))
        provider = Provider(mode="openai", model="m", base_url="https://example.test/v1", api_key="k")
        with patch("career_agent.providers.transport.asyncio.sleep", return_value=None):
            reply = self.complete(provider, client)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(reply["content"], "retried")

    def test_repeated_busy_status_maps_to_busy(self):
        client = FakeClient((429, {}), (503, {}))
        provider = Provider(mode="openai", model="m", base_url="https://example.test/v1", api_key="k")
        with patch("career_agent.providers.transport.asyncio.sleep", return_value=None):
            with self.assertRaises(ModelError) as caught:
                self.complete(provider, client)
        self.assertEqual(caught.exception.code, "model_busy")
        self.assertEqual(caught.exception.status_code, 503)

    def test_error_codes_are_stable(self):
        cases = [
            (401, "model_auth_failed", 502),
            (403, "model_auth_failed", 502),
            (400, "model_request_rejected", 502),
            (422, "model_request_rejected", 502),
            (500, "model_unavailable", 502),
        ]
        for status, code, expected in cases:
            provider = Provider(mode="openai", model="m", base_url="https://example.test/v1", api_key="k")
            with self.assertRaises(ModelError) as caught:
                self.complete(provider, FakeClient((status, {})))
            self.assertEqual((caught.exception.code, caught.exception.status_code), (code, expected))

    def test_timeout_and_transport_errors_are_mapped(self):
        provider = Provider(mode="openai", model="m", base_url="https://example.test/v1", api_key="k")
        with self.assertRaises(ModelError) as caught:
            self.complete(provider, FakeClient(httpx.TimeoutException("slow")))
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("model_timeout", 504))

        with self.assertRaises(ModelError) as caught:
            self.complete(provider, FakeClient(httpx.ConnectError("refused")))
        self.assertEqual(caught.exception.code, "model_unavailable")

    def test_unexpected_body_maps_to_invalid_response(self):
        provider = Provider(mode="openai", model="m", base_url="https://example.test/v1", api_key="k")
        with self.assertRaises(ModelError) as caught:
            self.complete(provider, FakeClient((200, [1, 2, 3])))
        self.assertEqual(caught.exception.code, "model_invalid_response")

        with self.assertRaises(ModelError) as caught:
            self.complete(provider, FakeClient((200, {"unexpected": True})))
        self.assertEqual(caught.exception.code, "model_invalid_response")

    def test_max_output_budget_is_validated(self):
        self.assertEqual(Provider(mode="demo").configuration_error(), None)
        self.assertIn("256", Provider(max_output=100).configuration_error())
        self.assertIn("8192", Provider(max_output="many").configuration_error())
        self.assertEqual(Provider(max_output="4096").max_output_tokens(), 4096)
        self.assertEqual(Provider(max_output=256).max_output_tokens(), 256)

    def test_catalog_applies_declared_max_output_tokens(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "models.json"
            path.write_text(json.dumps([{"id": "local", "provider": "openai_compatible",
                                         "model": "local-model", "base_url": "http://127.0.0.1:1234/v1",
                                         "supports_tools": True, "max_output_tokens": 3000}]),
                            encoding="utf-8")
            catalog = ModelCatalog(path)
        self.assertEqual(catalog.get("local").max_output_tokens(), 3000)
        self.assertEqual(catalog.list()[1]["max_output_tokens"], 3000)
        self.assertEqual(catalog.list()[0]["max_output_tokens"], 2048)


    def test_catalog_tolerates_a_byte_order_mark(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "models.json"
            path.write_text(json.dumps([{"id": "local", "provider": "openai_compatible",
                                         "model": "local-model", "base_url": "http://127.0.0.1:1234/v1",
                                         "supports_tools": True}]), encoding="utf-8-sig")
            self.assertIsInstance(ModelCatalog(path).get("local"), Provider)


if __name__ == "__main__":
    unittest.main()

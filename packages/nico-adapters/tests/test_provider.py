import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nico_adapters.models import ADAPTERS, openai_chat
from nico_adapters.models.catalog import ModelCatalog
from nico_adapters.models.provider import Provider
from nico_contracts.errors import ModelError


class ModelAdapterTests(unittest.TestCase):
    def test_registry_keeps_supported_protocols(self):
        self.assertIs(ADAPTERS["openai"], ADAPTERS["openai_compatible"])
        self.assertIn("anthropic", ADAPTERS)

    def test_local_compatible_provider_needs_no_api_key(self):
        provider = Provider(
            mode="openai_compatible", model="local-model",
            base_url="http://127.0.0.1:1234/v1",
        )
        self.assertIsNone(provider.configuration_error())
        endpoint, headers, payload = ADAPTERS["openai_compatible"].request(
            provider.model, [{"role": "user", "content": "hello"}], [], 2048,
        )
        self.assertEqual(endpoint, "chat/completions")
        self.assertEqual(headers, {})
        self.assertNotIn("tools", payload)

    def test_openai_and_deepseek_use_the_chat_completions_adapter(self):
        self.assertIs(ADAPTERS["openai"], ADAPTERS["deepseek"])
        with patch.dict(os.environ, {"MODEL_TEMPERATURE": ""}):
            openai = Provider(mode="openai", model="gpt-4o-mini",
                              base_url="https://api.openai.com", api_key="openai-test-key")
        self.assertEqual(openai.base_url, "https://api.openai.com/v1")
        self.assertEqual(openai.temperature_value(), 0.7)
        endpoint, _, payload = ADAPTERS["openai"].request(
            openai.model, [{"role": "user", "content": "hello"}], [], 2048,
            openai.temperature_value(),
        )
        self.assertEqual(endpoint, "chat/completions")
        self.assertEqual(payload["temperature"], 0.7)

        deepseek = Provider(mode="deepseek", model="deepseek-flash", api_key="deepseek-test-key")
        self.assertEqual(deepseek.base_url, "https://api.deepseek.com")
        self.assertIsNone(deepseek.temperature_value())
        self.assertIsNone(deepseek.configuration_error())

    def test_model_catalog_reads_only_the_named_provider_key(self):
        secret = "deepseek-test-key"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "models.json"
            path.write_text(json.dumps([{
                "id": "deepseek", "provider": "deepseek", "model": "deepseek-flash",
                "api_key_env": "DEEPSEEK_API_KEY", "supports_tools": True,
            }]), encoding="utf-8")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}, clear=False):
                catalog = ModelCatalog(path)
        self.assertEqual(catalog.get("deepseek").api_key, secret)
        self.assertNotIn(secret, repr(catalog.list()))

    def test_openai_stream_reassembles_text_and_tool_argument_fragments(self):
        chunks = [
            json.dumps({"choices": [{"delta": {"content": "hello "}, "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call-1", "type": "function",
                "function": {"name": "lookup", "arguments": "{\"query\":"},
            }]}, "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "function": {"arguments": "\"Python\"}"},
            }]}, "finish_reason": None}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
            json.dumps({"choices": [], "usage": {
                "prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7,
            }}),
            "[DONE]",
        ]
        deltas = []

        async def source():
            for chunk in chunks:
                yield chunk

        reply = asyncio.run(openai_chat.parse_stream(source(), deltas.append))
        self.assertEqual(deltas, ["hello "])
        self.assertEqual(reply["finish_reason"], "tool_calls")
        self.assertEqual(reply["tool_calls"][0]["function"]["arguments"], "{\"query\":\"Python\"}")
        self.assertEqual(reply["usage"]["total_tokens"], 7)

    def test_openai_stream_rejects_missing_done_marker(self):
        async def source():
            yield json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})

        with self.assertRaises(ModelError) as caught:
            asyncio.run(openai_chat.parse_stream(source(), lambda _: None))
        self.assertEqual(caught.exception.code, "model_invalid_response")


if __name__ == "__main__":
    unittest.main()

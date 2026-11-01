import unittest

from nico_adapters.models import ADAPTERS
from nico_adapters.models.provider import Provider


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


if __name__ == "__main__":
    unittest.main()

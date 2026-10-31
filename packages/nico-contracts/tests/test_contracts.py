import unittest

from nico_contracts import (
    ChannelDeliveryError,
    ChannelResult,
    ModelError,
    TextMessage,
    Usage,
    run_event,
)


class ContractTests(unittest.TestCase):
    def test_text_message_and_channel_result_are_stable_values(self):
        message = TextMessage("sender", "conversation", "message", "text")
        result = ChannelResult("session", "reply", duplicate=True)
        self.assertEqual(message.message_id, "message")
        self.assertTrue(result.duplicate)
        with self.assertRaises(AttributeError):
            message.text = "changed"

    def test_usage_accounts_for_valid_provider_values(self):
        usage = Usage()
        usage.request_count = 2
        usage.add({"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})
        usage.add({"total_tokens": True, "prompt_tokens": -1})
        self.assertEqual(usage.as_dict(), {
            "request_count": 2,
            "covered_requests": 1,
            "prompt_tokens": 5,
            "completion_tokens": 2,
            "total_tokens": 7,
        })
        self.assertEqual(usage.coverage(), "partial")

    def test_run_event_never_embeds_message_content(self):
        usage = Usage()
        usage.request_count = 1
        event = run_event(
            version=3,
            status="completed",
            error_code=None,
            provider="fake",
            model="model",
            prompt_version="prompt",
            context_chars=10,
            context_dropped=["CHAT_MEMORY"],
            tool_names=["read_material"],
            usage=usage,
            elapsed_ms=12,
        )
        self.assertEqual(event["status"], "completed")
        self.assertNotIn("content", event)

    def test_stable_errors_keep_codes_and_delivery_identifiers(self):
        model_error = ModelError("model_timeout", "timeout", 504)
        self.assertEqual((model_error.code, model_error.status_code), ("model_timeout", 504))
        delivery_error = ChannelDeliveryError("message", "delivery")
        self.assertEqual((delivery_error.message_id, delivery_error.delivery_id),
                         ("message", "delivery"))


if __name__ == "__main__":
    unittest.main()

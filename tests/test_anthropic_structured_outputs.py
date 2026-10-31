import json
import os
import unittest
from unittest.mock import patch

from capture_app.integrations import anthropic
from capture_app.integrations.openrouter import OpenRouterError


class FakeHTTPResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self.payload


class AnthropicStructuredOutputTests(unittest.TestCase):
    def test_end_turn_sends_schema_unchanged_and_returns_metadata(self):
        schema = {"type": "object", "required": ["ok"], "additionalProperties": False,
                  "properties": {"ok": {"type": "boolean"}}}
        response = FakeHTTPResponse({
            "content": [{"type": "text", "text": '{"ok":true}'}],
            "stop_reason": "end_turn", "usage": {"output_tokens": 42},
        })
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), \
             patch.object(anthropic.httpx, "post", return_value=response) as post:
            result = anthropic.messages_json("system", "user", max_tokens=100,
                                             output_schema=schema)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["output_config"], {
            "format": {"type": "json_schema", "schema": schema},
        })
        self.assertEqual(dict(result), {"ok": True})
        self.assertEqual(result.response_metadata,
                         {"stop_reason": "end_turn", "output_tokens": 42})

    def test_non_end_turn_is_rejected_before_parsing_and_preserves_full_response(self):
        for reason in ("max_tokens", "refusal", "tool_use"):
            with self.subTest(stop_reason=reason):
                response = FakeHTTPResponse({
                    "content": [{"type": "text", "text": '{"partial":'}],
                    "stop_reason": reason, "usage": {"output_tokens": 91},
                })
                with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), \
                     patch.object(anthropic.httpx, "post", return_value=response):
                    with self.assertRaises(OpenRouterError) as caught:
                        anthropic.messages_json("system", "user", max_tokens=100)
                error = caught.exception
                self.assertIn(f"stop_reason={reason}", str(error))
                self.assertIn("output_tokens=91", str(error))
                self.assertEqual(error.stop_reason, reason)
                self.assertEqual(error.output_tokens, 91)
                self.assertEqual(error.raw_response, response.text)
                self.assertEqual(error.raw_output, '{"partial":')

    def test_end_turn_invalid_json_keeps_full_raw_response(self):
        response = FakeHTTPResponse({
            "content": [{"type": "text", "text": '{"partial":'}],
            "stop_reason": "end_turn", "usage": {"output_tokens": 17},
        })
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), \
             patch.object(anthropic.httpx, "post", return_value=response):
            with self.assertRaises(OpenRouterError) as caught:
                anthropic.messages_json("system", "user")
        self.assertIn("stop_reason=end_turn", str(caught.exception))
        self.assertEqual(caught.exception.raw_response, response.text)
        self.assertEqual(caught.exception.raw_output, '{"partial":')


if __name__ == "__main__":
    unittest.main()

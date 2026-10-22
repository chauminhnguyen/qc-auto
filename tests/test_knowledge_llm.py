import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from capture_app.knowledge.llm import (
    CLAUDE_MAX_TOKENS,
    DEFAULT_CLAUDE_MODEL,
    ClaudeProvider,
    extract_sources,
    provider_from_args,
)


class KnowledgeClaudeProviderTests(unittest.TestCase):
    def _sdk(self, create):
        messages = SimpleNamespace(create=create)
        client = SimpleNamespace(messages=messages)
        anthropic = types.ModuleType("anthropic")
        anthropic.Anthropic = Mock(return_value=client)
        anthropic.RateLimitError = type("RateLimitError", (Exception,), {})
        anthropic.OverloadedError = type("OverloadedError", (Exception,), {})
        return anthropic, client

    def test_provider_from_args_uses_env_and_flag_model_override(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("capture_app.knowledge.llm.load_project_env"):
            default_provider = provider_from_args("claude")
        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "env-model"}, clear=True), \
                patch("capture_app.knowledge.llm.load_project_env"):
            env_provider = provider_from_args("claude")
            flag_provider = provider_from_args("claude", claude_model="flag-model")

        self.assertEqual(env_provider.model, "env-model")
        self.assertEqual(flag_provider.model, "flag-model")
        self.assertEqual(default_provider.model, DEFAULT_CLAUDE_MODEL)

    def test_openrouter_model_uses_env_with_cli_override(self):
        with patch.dict(os.environ, {"OPENROUTER_MODEL": "env/openrouter-model"}, clear=True), \
                patch("capture_app.knowledge.llm.load_project_env"):
            env_provider = provider_from_args("openrouter")
            flag_provider = provider_from_args("openrouter", model="flag/openrouter-model")

        self.assertEqual(env_provider.model, "env/openrouter-model")
        self.assertEqual(flag_provider.model, "flag/openrouter-model")

    def test_missing_api_key_is_clear_and_does_not_construct_sdk(self):
        anthropic, _ = self._sdk(Mock())
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=True), \
                patch.dict(sys.modules, {"anthropic": anthropic}):
            with self.assertRaisesRegex(ValueError, "ANTHROPIC_API_KEY"):
                ClaudeProvider().complete_json("system", "user")
        anthropic.Anthropic.assert_not_called()

    def test_parses_fenced_json_and_sends_explicit_messages_request(self):
        response = SimpleNamespace(content=[SimpleNamespace(
            type="text", text='```json\n{"entries": [], "questions": []}\n```')])
        anthropic, client = self._sdk(Mock(return_value=response))
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret-key"}), \
                patch.dict(sys.modules, {"anthropic": anthropic}):
            result = ClaudeProvider(model="test-model").complete_json("instructions", "source")

        self.assertEqual(result, {"entries": [], "questions": []})
        anthropic.Anthropic.assert_called_once_with(api_key="secret-key", max_retries=0)
        client.messages.create.assert_called_once()
        kwargs = client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "test-model")
        self.assertEqual(kwargs["max_tokens"], CLAUDE_MAX_TOKENS)
        self.assertIn("JSON only", kwargs["system"])
        self.assertEqual(kwargs["messages"], [{"role": "user", "content": "source"}])

    def test_retries_rate_limit_and_overload_with_exponential_backoff(self):
        rate_limit_error = type("RateLimitError", (Exception,), {})
        overloaded_error = type("OverloadedError", (Exception,), {})
        anthropic, client = self._sdk(Mock(side_effect=[
            rate_limit_error(),
            overloaded_error(),
            SimpleNamespace(content=[SimpleNamespace(type="text", text='{"ok": true}')]),
        ]))
        anthropic.RateLimitError = rate_limit_error
        anthropic.OverloadedError = overloaded_error
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret-key"}), \
                patch.dict(sys.modules, {"anthropic": anthropic}), \
                patch("capture_app.knowledge.llm.time.sleep") as sleep:
            result = ClaudeProvider(max_retries=2).complete_json("system", "user")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.messages.create.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0])

    def test_non_retryable_errors_are_not_retried(self):
        anthropic, client = self._sdk(Mock(side_effect=RuntimeError("bad request")))
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret-key"}), \
                patch.dict(sys.modules, {"anthropic": anthropic}), \
                patch("capture_app.knowledge.llm.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "bad request"):
                ClaudeProvider().complete_json("system", "user")

        client.messages.create.assert_called_once()
        sleep.assert_not_called()


class KnowledgeSourceExtractionTests(unittest.TestCase):
    class Catalog:
        by_id = {"APIs/shop.test/GET_items": object()}

        def prompt_rows(self):
            return [{"id": "APIs/shop.test/GET_items", "kind": "api"}]

    def test_drops_non_verbatim_quotes_and_invalid_source_markers(self):
        marker = "[SOURCE: jira SD-3 comment 101]"
        markdown = f"# Jira\n\n{marker}\nThe save button failed after checkout."

        class FakeProvider:
            def complete_json(self, _system, _user):
                return {"items": [
                    {"type": "bug", "title": "Save failure", "claim": "The save button failed.",
                     "evidence_quote": "The save button failed after checkout.", "source_marker": marker,
                     "symptom": "Save failed", "target_ids": ["APIs/shop.test/GET_items", "bad"]},
                    {"type": "decision", "title": "Invented", "claim": "Unsupported",
                     "evidence_quote": "This was invented.", "source_marker": marker},
                    {"type": "decision", "title": "Bad marker", "claim": "Unsupported",
                     "evidence_quote": "The save button failed after checkout.",
                     "source_marker": "[SOURCE: jira SD-3 comment 999]"},
                ], "questions": ["Was this reproduced?"]}

        result = extract_sources(markdown, self.Catalog(), FakeProvider())
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["source_marker"], marker)
        self.assertEqual(result["items"][0]["target_ids"], ["APIs/shop.test/GET_items"])
        self.assertEqual(result["rejected_items"], 2)
        self.assertEqual(result["questions"], ["Was this reproduced?"])

    def test_splits_oversized_input_at_source_blocks_and_preserves_markers(self):
        markdown = ("# Jira\n\n[SOURCE: jira SD-3 issue]\n" + "issue body " * 60
                    + "\n\n[SOURCE: jira SD-3 comment 101]\n" + "comment body " * 60)

        class FakeProvider:
            def __init__(self):
                self.inputs = []

            def complete_json(self, _system, user):
                import json
                self.inputs.append(json.loads(user)["source_blocks"])
                return {"items": [], "questions": []}

        provider = FakeProvider()
        result = extract_sources(markdown, self.Catalog(), provider, max_chars=256)
        self.assertGreater(result["batches"], 1)
        self.assertEqual(len(provider.inputs), result["batches"])
        joined = "\n".join(provider.inputs)
        self.assertIn("[SOURCE: jira SD-3 issue]", joined)
        self.assertIn("[SOURCE: jira SD-3 comment 101]", joined)
        self.assertIn("issue body", joined)
        self.assertIn("comment body", joined)


if __name__ == "__main__":
    unittest.main()

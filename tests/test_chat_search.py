import argparse
import tempfile
import unittest
from pathlib import Path

from capture_app.chat.config import ChatConfig
from capture_app.chat.db import ChatDB


class ChatSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = ChatDB(Path(self.temp_dir.name) / "chat.sqlite")
        self.db.upsert_chunk({
            "id": "checkout-flow",
            "source_path": "Flows/checkout.md",
            "note_type": "flow",
            "title": "Checkout flow",
            "feature": "Checkout",
            "status": "unknown",
            "owner": "crawler",
            "linked_entities": [],
            "content_hash": "test-hash",
            "body": "The checkout flow saves billing and shipping before confirming the order.",
        })

    def tearDown(self):
        self.db.close()
        self.temp_dir.cleanup()

    def test_natural_language_query_matches_content_terms(self):
        results = self.db.search("What is known about checkout?")

        self.assertTrue(results)
        self.assertEqual(results[0]["source_path"], "Flows/checkout.md")

    def test_query_with_only_stopwords_returns_no_results(self):
        self.assertEqual(self.db.search("what is this about"), [])


class ChatConfigArgsTests(unittest.TestCase):
    def test_from_args_uses_cli_configuration_values(self):
        args = argparse.Namespace(
            vault=Path("sample-vault"),
            db=Path("sample-chat.sqlite"),
            provider="openrouter",
            model="test-model",
            api_base="https://example.test/chat/completions",
            api_key="test-key",
            temperature=0.4,
            max_tokens=700,
        )

        config = ChatConfig.from_args(args)

        self.assertEqual(config.vault_path, Path("sample-vault").resolve())
        self.assertEqual(config.db_path, Path("sample-chat.sqlite"))
        self.assertEqual(config.provider, "openrouter")
        self.assertEqual(config.model, "test-model")
        self.assertEqual(config.api_base, "https://example.test/chat/completions")
        self.assertEqual(config.api_key, "test-key")
        self.assertEqual(config.temperature, 0.4)
        self.assertEqual(config.max_tokens, 700)


if __name__ == "__main__":
    unittest.main()

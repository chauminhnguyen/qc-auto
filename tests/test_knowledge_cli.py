import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from capture_app.knowledge.cli import main, parser, validate_ingest_sources


class KnowledgeIngestSourceCliTests(unittest.TestCase):
    def test_notes_and_jira_source_combinations_parse_without_jira_provider(self):
        cases = (
            (["--notes", "Notes-raw", "--provider", "local"], Path("Notes-raw"), []),
            (["--jira", "PROJ-123"], None, ["PROJ-123"]),
            (["--notes", "Notes-raw", "--provider", "local", "--jira", "PROJ-123"],
             Path("Notes-raw"), ["PROJ-123"]),
            (["--jira", "PROJ-123", "--jira", "https://example.atlassian.net/browse/PROJ-124"],
             None, ["PROJ-123", "https://example.atlassian.net/browse/PROJ-124"]),
        )
        for source_args, expected_notes, expected_jira in cases:
            with self.subTest(source_args=source_args):
                args = parser().parse_args(["ingest", *source_args])
                self.assertEqual(args.notes, expected_notes)
                self.assertEqual(args.jira, expected_jira)
                validate_ingest_sources(args.notes, args.jira, args.provider)

    def test_notes_require_provider_and_jira_only_rejects_provider(self):
        with self.assertRaisesRegex(ValueError, "--provider is required"):
            validate_ingest_sources(Path("Notes-raw"), [], None)
        with self.assertRaisesRegex(ValueError, "does not accept --provider"):
            validate_ingest_sources(None, ["PROJ-123"], "openrouter")

    def test_missing_sources_has_clear_usage_error_before_provider_setup(self):
        stderr = io.StringIO()
        with patch("capture_app.knowledge.jira.JiraClient.from_env") as jira_client:
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    main(["ingest"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("provide --notes and/or --jira", stderr.getvalue())
        jira_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()

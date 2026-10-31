from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from capture_app.workflows import testcase_gen


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sql", ".ps1", ".sh", ".xml",
}
SKIP_DIRS = {
    ".git", ".venv", "env", "node_modules", "__pycache__", ".pytest_cache",
    ".capture_index", ".chat_index", ".obsidian",
}


def _mojibake_markers() -> tuple[str, ...]:
    # Construct the codepoints so this guard does not contain the bad text itself.
    return (
        "".join(chr(value) for value in (0x00E2, 0x20AC, 0x201C)),
        "".join(chr(value) for value in (0x00E2, 0x20AC, 0x201D)),
        chr(0x00C3),
    )


class SourceEncodingTests(unittest.TestCase):
    def test_source_tree_has_no_known_mojibake(self):
        markers = _mojibake_markers()
        offenders = []
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in SOURCE_SUFFIXES:
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if any(marker in text for marker in markers):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
        self.assertEqual(offenders, [], f"Known mojibake found in: {offenders}")

    def test_legacy_cache_and_progress_statuses_are_normalized_on_read(self):
        marker = _mojibake_markers()[0]
        old_status = f"Blocked {marker} needs clarification: old cached note"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache_path = root / "cache.json"
            cache_path.write_text(json.dumps({
                "version": testcase_gen._ResponseCache.VERSION,
                "entries": {"key": {"review_status": old_status}},
            }), encoding="utf-8")
            cache = testcase_gen._ResponseCache(cache_path)
            self.assertEqual(cache.entries["key"]["review_status"],
                             f"{testcase_gen.BLOCKED_REVIEW_STATUS}: old cached note")

            progress_path = root / "progress.json"
            progress_path.write_text(json.dumps({
                "fingerprint": "fingerprint", "attempts": {},
                "processed": {"item": {"review_status": old_status}},
            }), encoding="utf-8")
            progress = testcase_gen._load_brd_progress(progress_path, "fingerprint")
            self.assertEqual(progress["processed"]["item"]["review_status"],
                             f"{testcase_gen.BLOCKED_REVIEW_STATUS}: old cached note")

    def test_blocked_status_prefix_accepts_additional_explanation(self):
        self.assertTrue(testcase_gen._is_blocked_status("blocked: model supplied detail"))
        self.assertTrue(testcase_gen._is_blocked_status("  BLOCKED for further review"))
        self.assertFalse(testcase_gen._is_blocked_status("Ready for review"))


if __name__ == "__main__":
    unittest.main()

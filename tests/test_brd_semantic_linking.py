from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capture_app.brd_ingest.linking import resolve_links


class SemanticBrdLinkingTests(unittest.TestCase):
    def test_provider_uses_shared_llm_provider_environment_setting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "vault"
            source = Path(temp_dir) / "BRD.md"
            source.write_text(
                "# Scheduling\n\n| ID | Requirement | Priority |\n|---|---|---|\n"
                "| BR-01 | Patients join a video visit. | Must |\n",
                encoding="utf-8",
            )
            api_dir = root / "APIs" / "clinic.test"
            api_dir.mkdir(parents=True)
            (api_dir / "POST_video_join.md").write_text(
                "---\nmethod: POST\npath_template: /video/join\ndomain: clinic.test\n---\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic",
                                         "TESTCASE_LLM_PROVIDER": "openrouter"}), \
                    patch("capture_app.integrations.anthropic.messages_json",
                          return_value={"links": []}) as anthropic_chat, \
                    patch("capture_app.integrations.openrouter.chat_json") as openrouter_chat:
                result = resolve_links(root, [source], use_llm=True)

        anthropic_chat.assert_called_once()
        openrouter_chat.assert_not_called()
        self.assertTrue(result["semantic_linking"]["enabled"])

    def test_semantic_links_are_validated_against_catalog_and_written_to_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "vault"
            source_dir = Path(temp_dir) / "source"
            api_dir = root / "APIs" / "citycare.test"
            param_dir = root / "Params"
            api_dir.mkdir(parents=True)
            param_dir.mkdir(parents=True)
            source_dir.mkdir()
            source = source_dir / "BRD-Clinic.md"
            source.write_text(
                "# Clinic Scheduling\n\n| ID | Requirement | Priority |\n|---|---|---|\n"
                "| BR-01 | A patient chooses an available physician's time. | Must |\n",
                encoding="utf-8",
            )
            api_path = api_dir / "POST_appointments_reserve.md"
            api_path.write_text(
                "---\nmethod: POST\npath_template: /appointments/reserve\n"
                "domain: citycare.test\n---\n\n## Sample Payload\nprivate-value\n",
                encoding="utf-8",
            )
            param_path = param_dir / "appointment__clinician_id.md"
            param_path.write_text(
                "---\nresource: appointment\ncanonical_name: clinician_id\n---\n"
                "\n## Used by\n- [[POST_appointments_reserve]]\n",
                encoding="utf-8",
            )
            api_rel = api_path.relative_to(root).as_posix()
            param_rel = param_path.relative_to(root).as_posix()

            def fake_chat(_system, user_prompt, **_kwargs):
                self.assertNotIn("private-value", user_prompt)
                return {"links": [{
                    "source_file": "BRD-Clinic.md",
                    "requirement_id": "BR-01",
                    "entities": [
                        {"path": api_rel,
                         "evidence": "This route reserves appointments with clinicians, matching the booking action."},
                        {"path": param_rel,
                         "evidence": "The clinician identifier selects the clinician for the appointment."},
                        {"path": "APIs/fake/POST_hallucinated.md",
                         "evidence": "This is a fabricated path and must be rejected by validation."},
                    ],
                }]}

            with patch("capture_app.integrations.openrouter.chat_json", side_effect=fake_chat):
                result = resolve_links(root, [source], use_llm=True)

            links = result["requirement_entity_links"]
            self.assertEqual(len(links), 1)
            normalize = lambda path: path.replace("\\", "/")
            self.assertEqual({normalize(entity["path"]) for entity in links[0]["entities"]},
                             {api_rel, param_rel})
            self.assertTrue(all(entity["match_basis"] == "llm_semantic"
                                for entity in links[0]["entities"]))
            self.assertEqual(result["semantic_linking"], {"enabled": True, "accepted_links": 2})
            persisted = json.loads((root / "BRDs" / "reports" / "links.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["requirement_entity_links"], links)

    def test_semantic_model_can_abstain_for_unrelated_entities(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "vault"
            source_dir = Path(temp_dir) / "source"
            api_dir = root / "APIs" / "shop.test"
            api_dir.mkdir(parents=True)
            source_dir.mkdir()
            source = source_dir / "BRD-Clinic.md"
            source.write_text(
                "# Telehealth\n\n| ID | Requirement | Priority |\n|---|---|---|\n"
                "| BR-01 | Patients join a video consultation with a clinician. | Must |\n",
                encoding="utf-8",
            )
            (api_dir / "POST_checkout_confirm.md").write_text(
                "---\nmethod: POST\npath_template: /checkout/confirm\ndomain: shop.test\n---\n",
                encoding="utf-8",
            )

            with patch("capture_app.integrations.openrouter.chat_json", return_value={"links": [{
                "source_file": "BRD-Clinic.md", "requirement_id": "BR-01", "entities": [],
            }]}) as chat:
                result = resolve_links(root, [source], use_llm=True)

            self.assertEqual(result["requirement_entity_links"], [])
            self.assertEqual(len(result["unresolved_references"]), 1)
            self.assertEqual(result["semantic_linking"]["accepted_links"], 0)
            chat.assert_called_once()

    def test_llm_errors_preserve_deterministic_link_results(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "vault"
            source_dir = Path(temp_dir) / "source"
            api_dir = root / "APIs" / "shop.test"
            api_dir.mkdir(parents=True)
            source_dir.mkdir(parents=True)
            source = source_dir / "BRD-Checkout.md"
            source.write_text(
                "# Checkout\n\n| ID | Requirement | Priority |\n|---|---|---|\n"
                "| BR-01 | Submit POST_checkout_order after checkout. | Must |\n",
                encoding="utf-8",
            )
            (api_dir / "POST_checkout_order.md").write_text("---\nmethod: POST\n---\n", encoding="utf-8")
            with patch("capture_app.integrations.openrouter.chat_json", side_effect=RuntimeError("offline")):
                result = resolve_links(root, [source], use_llm=True)

        self.assertEqual(len(result["requirement_entity_links"]), 1)
        self.assertEqual(result["requirement_entity_links"][0]["entities"][0]["match_basis"], "exact_id")


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import json

from capture_app.knowledge.changes import apply_change, propose, review_change
from capture_app.knowledge.validation import validate_vault
from capture_app.storage.vault import VaultWriter, canonical_param_name, template_url_path


class KnowledgeValidationTests(unittest.TestCase):
    def test_template_and_param_id_normalization(self):
        self.assertEqual(template_url_path("/catalog/14/1/1"), "/catalog/{productId}/1/1")
        self.assertEqual(template_url_path("/orderdetails/2394449"), "/orderdetails/{orderId}")
        self.assertEqual(canonical_param_name("product_attribute_74_3_28"), "product_attribute")
        self.assertEqual(canonical_param_name("addtocart_74.EnteredQuantity"), "addtocart_EnteredQuantity")
        self.assertEqual(canonical_param_name("EnteredQuantity"), "EnteredQuantity")

    def test_validator_reports_dangling_links_and_metadata_findings(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            (vault / "APIs").mkdir()
            (vault / "Params").mkdir()
            (vault / "Knowledge" / "Reviewed").mkdir(parents=True)
            (vault / "APIs" / "valid.md").write_text("# API\n", encoding="utf-8")
            (vault / "Params" / "raw.md").write_text(
                "## Definition\n_Not yet analyzed — run `enrich.py`._\n", encoding="utf-8")
            (vault / "Knowledge" / "Reviewed" / "rule.md").write_text(
                "---\nstatus: unverified\nreview_state: draft\n---\n\n"
                "[[APIs/valid]] [[APIs/missing]] [[Params/raw]]\n", encoding="utf-8")
            (vault / "Notes.md").write_text("# Other note\n", encoding="utf-8")
            (vault / "source.md").write_text("See [[Notes]]\n", encoding="utf-8")
            report = validate_vault(vault)
            kinds = [finding["kind"] for finding in report["findings"]]
            self.assertIn("dangling_link", kinds)
            self.assertIn("review_state_folder_mismatch", kinds)
            self.assertIn("unenriched_param", kinds)
            self.assertNotIn("dangling_link", [item["kind"] for item in report["findings"]
                                                if item.get("target") == "Notes"])

    def test_regeneration_groups_numeric_routes_and_parameter_names(self):
        with tempfile.TemporaryDirectory() as temp:
            writer = VaultWriter(Path(temp))
            for product_id, attribute_id, value_id, value in ((74, 3, 28, "86"), (75, 3, 33, "102")):
                writer.ingest({
                    "method": "POST",
                    "url": f"https://shop.example.test/addproducttocart/details/{product_id}/1",
                    "status": 200,
                    "request_headers": {"Content-Type": "application/x-www-form-urlencoded"},
                    "response_headers": {"Content-Type": "application/json"},
                    "payload": f"product_attribute_{product_id}_{attribute_id}_{value_id}={value}",
                    "response_body": "{}",
                    "entry_point": "Test",
                })
            writer.regenerate_capture_notes()
            api_note = Path(temp) / "APIs" / "shop.example.test" / "POST_addproducttocart_details_productId_1.md"
            param_note = Path(temp) / "Params" / "shop.example.test_addproducttocart__product_attribute.md"
            self.assertTrue(api_note.exists())
            content = param_note.read_text(encoding="utf-8")
            self.assertIn("product_attribute_74_3_28", content)
            self.assertIn("product_attribute_75_3_33", content)
            self.assertIn("86", content)
            self.assertIn("102", content)
            self.assertTrue((Path(temp) / "Params" / "shop.example.test_addproducttocart__product_attribute_74_3_28.md").exists())
            writer.conn.close()

    def test_proposal_review_and_versioned_apply_in_isolated_vault(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            writer = VaultWriter(vault)
            writer.ingest({
                "method": "GET", "url": "https://shop.example.test/orders", "status": 200,
                "request_headers": {}, "response_headers": {"Content-Type": "application/json"},
                "payload": "", "response_body": '{"orders":[]}', "entry_point": "Test",
            })
            entity_id = "APIs/shop.example.test/GET_orders"
            writer.conn.close()

            requirements = vault / "BRDs" / "entries"
            requirements.mkdir(parents=True)
            (requirements / "BRD-12_Orders-BR-1.1.md").write_text(
                "---\ntype: requirement\norigin: brd\nstatus: approved\n"
                "id: BR-1.1\nsource: BRD-12_Orders.md#Requirements\n"
                "acceptance_criteria: [\"Orders endpoint should return 200 for valid requests.\"]\n---\n",
                encoding="utf-8")
            ticket = "# Orders status issue\n\nObserved 503 from orders endpoint."

            class FakeProvider:
                def complete_json(self, _system, user):
                    context = json.loads(user)
                    req_id = context["candidate_requirements"][0]["id"]
                    return {"summary": "Orders endpoint should return 200 for valid requests.",
                            "classification": "defect", "open_questions": [], "po_conflicts": [],
                            "confidence": 0.9, "test_impacts": [],
                            "changes": [{"entity_id": entity_id, "field": "expected_status",
                                         "proposed": 200,
                                         "evidence_quotes": ["Observed 503 from orders endpoint."],
                                         "requirement_ids": [req_id], "rationale": "Test fixture"}]}

            with patch("capture_app.knowledge.changes.provider_from_args", return_value=FakeProvider()):
                proposal = propose(vault, ticket, "TEST-1", "local")
            self.assertEqual(proposal["status"], "ready_for_review")
            review_change(vault, proposal["proposal_id"], "approve", "Reviewer", "Verified fixture requirement")
            applied = apply_change(vault, proposal["proposal_id"], "Reviewer", "Apply fixture")
            self.assertEqual(applied["status"], "applied")
            import sqlite3
            conn = sqlite3.connect(vault / "Knowledge" / "Changes" / "_state" / "changes.sqlite")
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM contract_versions").fetchone()[0], 1)
            finally:
                conn.close()
            updated_note = (vault / f"{entity_id}.md").read_text(encoding="utf-8")
            self.assertIn('"orders": []', updated_note)
            self.assertIn("## Approved expected contract", updated_note)


if __name__ == "__main__":
    unittest.main()

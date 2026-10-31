import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from capture_app.workflows import testcase_gen


class FakeCache:
    enabled = True

    def __init__(self, response_factory=None):
        self.entries = {}
        self.calls = 0
        self.discarded = 0
        self.prompts = []
        self.token_limits = []
        self.response_factory = response_factory or self.response

    def cache_key(self, system_prompt, user_prompt, model, max_tokens):
        return hashlib.sha256(f"{max_tokens}:{user_prompt}".encode()).hexdigest()

    def chat_json(self, system_prompt, user_prompt, model, max_tokens):
        self.prompts.append(user_prompt)
        self.token_limits.append(max_tokens)
        key = self.cache_key(system_prompt, user_prompt, model, max_tokens)
        if key in self.entries:
            return self.entries[key]
        self.calls += 1
        response = self.response_factory(json.loads(user_prompt)["rows"])
        self.entries[key] = response
        return response

    def discard(self, system_prompt, user_prompt, model, max_tokens):
        self.discarded += 1
        self.entries.pop(self.cache_key(system_prompt, user_prompt, model, max_tokens), None)

    @staticmethod
    def response(items):
        results = []
        for item in items:
            facts = item["observed_facts"]
            candidates = item["brd_candidates"]
            if facts["visible_messages"]:
                message = facts["visible_messages"][0]
                text = f'The page displays "{message}".'
                refs = ["ui.message.0"]
            elif candidates:
                text = f"The recorded action {item['test_case_name']} meets the stated requirement."
                refs = [f"brd:{candidates[0]['candidate_id']}"]
            else:
                page = facts.get("page_context", "page context not observed")
                action = facts.get("action", {}).get("label") or item["test_case_name"]
                text = f"No verifiable result was observed for {action} on {page}."
                refs = ["absence:no_verifiable_result"]
            results.append({
                "row_key": item["row_key"],
                "selected_brd_candidate_id": candidates[0]["candidate_id"] if candidates else None,
                "expected_result_lines": [{"text": text, "source_refs": refs}],
                "possible_defect": False,
                "evidence_note": "",
            })
        return {"results": results}


def make_row(key="run:1", name="Save order", facts=None):
    return {
        "_transition_key": key,
        "Test Case Name": name,
        "Precondition": "An order is ready to save.",
        "Category": "observed-flow",
        "_transition_actions": [{"kind": "click", "role": "button", "name": name, "value": ""}],
        "_transition_apis": [],
        "_observed_facts": facts or {
            "element_label": name,
            "element_role": "button",
            "entity": "order",
            "source_context": "orders page",
            "messages": [],
            "title_after": "not observed",
            "heading_after": [],
            "before_url": "https://unrelated.example/orders",
            "after_url": "https://unrelated.example/orders",
        },
        "Expected Result": "placeholder",
        "Technical Evidence": "POST | 201 | /orders/save",
    }


class LiveExpectedResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="live-expected-")
        self.addCleanup(self.temp.cleanup)
        self.vault = SimpleNamespace(vault_path=Path(self.temp.name))

    def generate(self, rows, cache=None, requirements=None, max_calls=7, max_tokens=None):
        with patch.object(testcase_gen, "_load_brd_requirements", return_value=requirements or []):
            options = {"brd_db_path": None, "max_calls": max_calls}
            if max_tokens is not None:
                options["max_tokens"] = max_tokens
            return testcase_gen.generate_live_run_expected_results(
                self.vault, rows, cache or FakeCache(), **options,
            )

    def test_generates_no_oracle_result_and_leaves_technical_evidence_separate(self):
        row = make_row()
        self.generate([row])
        self.assertIn("No verifiable result", row["Expected Result"])
        self.assertNotIn("POST", row["Expected Result"])
        self.assertEqual(row["Technical Evidence"], "POST | 201 | /orders/save")
        self.assertIn("No clear BRD match", row["Evidence/Note"])

    def test_prompt_is_site_independent_and_carries_route_page_and_precondition(self):
        row = make_row(name="Confirm booking", facts={
            "element_label": "Confirm booking", "element_role": "button", "entity": "booking",
            "source_context": "booking page", "messages": [], "title_after": "not observed",
            "heading_after": [], "before_url": "https://alpha.invalid/book",
            "after_url": "https://beta.invalid/confirmation",
        })
        another = make_row("run:2", "Submit repair", facts={
            "element_label": "Submit repair", "element_role": "button", "entity": "repair",
            "source_context": "repairs page", "messages": [], "title_after": "not observed",
            "heading_after": [], "before_url": "https://gamma.invalid/repairs/new",
            "after_url": "https://gamma.invalid/repairs/new",
        })
        cache = FakeCache()
        self.generate([row, another], cache)
        prompt = cache.prompts[0]
        self.assertIn("Confirm booking", row["Expected Result"])
        self.assertIn("Submit repair", another["Expected Result"])
        self.assertIn("https://alpha.invalid/book", prompt)
        self.assertIn("https://gamma.invalid/repairs/new", prompt)
        self.assertIn("An order is ready to save.", prompt)
        # Different page names and routes pass through the same generic prompt.
        self.assertNotIn("demo", testcase_gen.OBSERVED_RESULT_SYSTEM_PROMPT.casefold())

    def test_clear_brd_candidate_and_possible_defect_are_recorded(self):
        requirement = {
            "statement": "The order confirmation screen shows the saved order number.",
            "requirement_id": "ORD-4", "traceability_key": "orders.md/ORD-4",
            "source_file": "orders.md", "source_display": "BRDs/orders.md", "section": "Orders",
        }
        row = make_row(name="Save order")

        def conflict(items):
            item = items[0]
            candidate = item["brd_candidates"][0]
            return {"results": [{
                "row_key": item["row_key"], "selected_brd_candidate_id": candidate["candidate_id"],
                "expected_result_lines": [{
                    "text": "The confirmation screen shows the saved order number.",
                    "source_refs": [f"brd:{candidate['candidate_id']}"]}],
                "possible_defect": True,
                "evidence_note": "Possible defect: the captured page did not show the required order number.",
            }]}

        cache = FakeCache(conflict)
        self.generate([row], cache, requirements=[requirement])
        self.assertEqual(row["_selected_brd_candidate_id"], "orders.md/ORD-4")
        self.assertTrue(row["Evidence/Note"].startswith("Possible defect:"))
        self.assertIn("BRD reference:", row["Evidence/Note"])

    def test_selected_brd_reference_is_added_to_line_provenance(self):
        item = testcase_gen._live_result_prompt_item(make_row(), [{
            "candidate_id": "rules.md/R-2", "requirement_id": "rules.md::R-2",
            "selection_key": "C1", "text": "Orders can be saved.",
            "source": "BRDs/rules.md", "source_file": "rules.md",
        }])
        response = {"results": [{
            "row_key": item["row_key"], "selected_brd_candidate_id": "C1",
            "expected_result_lines": [{
                "text": "The order is saved.", "source_refs": ["ui.action"],
            }],
            "possible_defect": False, "evidence_note": "",
        }]}
        validated = testcase_gen._validate_live_result_batch(response, [item])
        self.assertEqual(validated[item["row_key"]]["selected_brd_candidate_id"], "rules.md/R-2")

    def test_exact_visible_message_is_preserved_and_quote_is_banned_term_exempt(self):
        message = "API request failed"
        row = make_row(facts={
            "element_label": "Submit", "element_role": "button", "entity": "order",
            "source_context": "orders page", "messages": [message],
            "title_after": "not observed", "heading_after": [],
        })
        self.generate([row])
        self.assertIn(f'"{message}"', row["Expected Result"])
        self.assertEqual(testcase_gen._lint_live_expected_results([row]), [])
        row["Expected Result"] = f'The API displays "{message}".'
        self.assertTrue(any("technical wording" in error
                            for error in testcase_gen._lint_live_expected_results([row])))

    def test_ambiguous_or_missing_brd_match_is_not_selected(self):
        requirements = [
            {"statement": "Customers can save a favorite address.", "requirement_id": "A",
             "traceability_key": "a.md/A", "source_file": "a.md"},
            {"statement": "Customers can save a favorite payment method.", "requirement_id": "B",
             "traceability_key": "b.md/B", "source_file": "b.md"},
        ]
        # A fake that declines both near matches, as required for ambiguity.
        def decline(items):
            return {"results": [{
                "row_key": item["row_key"], "selected_brd_candidate_id": None,
                "expected_result_lines": [{
                    "text": f"No verifiable result was observed for {item['observed_facts']['action']['label']} "
                            f"on {item['observed_facts']['page_context']}.",
                    "source_refs": ["absence:no_verifiable_result"]}],
                "possible_defect": False, "evidence_note": "",
            } for item in items]}

        row = make_row(name="Save a favorite")
        self.generate([row], FakeCache(decline), requirements=requirements)
        self.assertIsNone(row["_selected_brd_candidate_id"])

    def test_missing_label_uses_recorded_action_kind_and_control_role(self):
        row = make_row(facts={
            "element_label": "not observed", "element_role": "not observed",
            "entity": "not observed", "source_context": "the current page",
            "messages": [], "title_after": "not observed", "heading_after": [],
        })
        row["_transition_actions"] = [{"kind": "fill", "role": "textbox", "name": "", "value": ""}]
        item = testcase_gen._live_result_prompt_item(row, [])
        self.assertEqual(item["observed_facts"]["action"]["label"], "not observed")
        self.assertEqual(item["observed_facts"]["action"]["kind"], "fill")
        self.assertEqual(item["observed_facts"]["action"]["position_in_recorded_route"], 1)
        response = {"results": [{
            "row_key": item["row_key"], "selected_brd_candidate_id": None,
            "expected_result_lines": [{
                "text": "No verifiable result was captured for entering text in the text box on the current page.",
                "source_refs": ["absence:no_verifiable_result"],
            }],
            "possible_defect": False, "evidence_note": "",
        }]}
        self.assertIn(item["row_key"], testcase_gen._validate_live_result_batch(response, [item]))

    def test_generic_page_context_is_added_when_llm_omits_it(self):
        row = make_row(facts={
            "element_label": "not observed", "element_role": "not observed",
            "entity": "not observed", "source_context": "the current page",
            "messages": [], "title_after": "not observed", "heading_after": [],
        })
        row["_transition_actions"] = [{"kind": "fill", "role": "textbox", "name": "", "value": ""}]

        def omits_context(items):
            item = items[0]
            return {"results": [{
                "row_key": item["row_key"], "selected_brd_candidate_id": None,
                "expected_result_lines": [{
                    "text": "No verifiable result was observed for entering text in the text box.",
                    "source_refs": ["absence:no_verifiable_result"],
                }],
                "possible_defect": False, "evidence_note": "",
            }]}

        self.generate([row], FakeCache(omits_context))
        self.assertTrue(row["Expected Result"].endswith("on the current page."))

    def test_generic_page_context_accepts_screen_or_form_wording(self):
        row = make_row(facts={
            "element_label": "Save order", "element_role": "button", "entity": "order",
            "source_context": "the current page", "messages": [],
            "title_after": "not observed", "heading_after": [],
        })
        item = testcase_gen._live_result_prompt_item(row, [])
        response = {"results": [{
            "row_key": item["row_key"], "selected_brd_candidate_id": None,
            "expected_result_lines": [{
                "text": "No verifiable result was observed for Save order on this screen.",
                "source_refs": ["absence:no_verifiable_result"],
            }],
            "possible_defect": False, "evidence_note": "",
        }]}
        self.assertIn(item["row_key"], testcase_gen._validate_live_result_batch(response, [item]))

    def test_generic_context_without_page_word_is_validated_and_normalized_later(self):
        row = make_row(facts={
            "element_label": "Save order", "element_role": "button", "entity": "order",
            "source_context": "the current page", "messages": [],
            "title_after": "not observed", "heading_after": [],
        })
        item = testcase_gen._live_result_prompt_item(row, [])
        response = {"results": [{
            "row_key": item["row_key"], "selected_brd_candidate_id": None,
            "expected_result_lines": [{
                "text": "No verifiable result was observed for Save order.",
                "source_refs": ["absence:no_verifiable_result"],
            }],
            "possible_defect": False, "evidence_note": "",
        }]}
        self.assertIn(item["row_key"], testcase_gen._validate_live_result_batch(response, [item]))

    def test_incomplete_batch_fails_and_invalid_cached_response_is_discarded(self):
        row = make_row()

        def incomplete(items):
            return {"results": []}

        cache = FakeCache(incomplete)
        output = Path(self.temp.name) / "must-not-exist.xlsx"
        with self.assertRaisesRegex(ValueError, "workbook was not written"):
            self.generate([row], cache)
        self.assertEqual(cache.discarded, 1)
        self.assertFalse(output.exists())

    def test_cli_does_not_write_workbook_when_expected_result_batch_is_incomplete(self):
        import sys
        from unittest.mock import patch

        def incomplete(items):
            return {"results": []}

        output = Path(self.temp.name) / "cases.xlsx"
        output.write_bytes(b"existing workbook")
        vault = SimpleNamespace(vault_path=Path(self.temp.name), conn=None)
        with patch.object(testcase_gen, "VaultWriter", return_value=vault), \
             patch.object(testcase_gen, "generate_from_live_run", return_value=[make_row()]), \
             patch.object(testcase_gen, "_ResponseCache", return_value=FakeCache(incomplete)), \
             patch.object(testcase_gen, "_load_brd_requirements", return_value=[]), \
             patch.object(sys, "argv", ["testcase_gen", "--out", str(output)]):
            with self.assertRaises(SystemExit):
                testcase_gen.main()
        self.assertEqual(output.read_bytes(), b"existing workbook")

    def test_batch_call_budget_counts_only_uncached_batches_and_cache_reuses(self):
        rows = [make_row(f"run:{i}", f"Action {i}") for i in range(45)]
        cache = FakeCache()
        self.generate(rows, cache, max_calls=3)
        self.assertEqual(cache.calls, 3)
        self.generate(rows, cache, max_calls=1)
        self.assertEqual(cache.calls, 3)

    def test_expected_result_token_limit_is_forwarded_to_the_llm_cache(self):
        cache = FakeCache()
        self.generate([make_row()], cache, max_tokens=10000)
        self.assertEqual(cache.token_limits, [10000])

    def test_duplicate_results_are_linted(self):
        first, second = make_row("r:1"), make_row("r:2")
        first["Expected Result"] = second["Expected Result"] = "The order is saved."
        self.assertTrue(any("Identical expected results" in error
                            for error in testcase_gen._lint_live_expected_results([first, second])))

    def test_duplicate_results_for_recorded_repeated_actions_get_distinct_ordinals(self):
        rows = [make_row("r:1"), make_row("r:2"), make_row("r:3")]
        for row, index in zip(rows, (1, 2, 3)):
            row["_observed_facts"]["element_label"] = "not observed"
            row["Expected Result"] = "No verifiable result was observed on the current page."
            row["Expected Result (Plain Language)"] = row["Expected Result"]
            row["_transition_actions"] = [{
                "kind": "click", "role": "button", "name": "Add to cart", "dup_index": index,
            }]
        testcase_gen._disambiguate_repeated_action_results(rows)
        self.assertEqual(testcase_gen._lint_live_expected_results(rows), [])
        self.assertIn("third recorded", rows[1]["Expected Result"])
        self.assertIn("fourth recorded", rows[2]["Expected Result"])


if __name__ == "__main__":
    unittest.main()

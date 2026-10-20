import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from capture_app.safety_gate.decisions import DecisionStore, endpoint_pattern, redact_url
from capture_app.safety_gate.gate import WriteSafeGate, _tag_create_body
from capture_app.safety_gate.overlay import MockOverlay
from capture_app.safety_gate import review as review_module


class FakeContext:
    def route(self, pattern, handler):
        self.pattern = pattern
        self.handler = handler


class FakeRoute:
    def __init__(self, method, url, body="", headers=None, fetch_response=None):
        self.request = SimpleNamespace(method=method, url=url, post_data=body, headers=headers or {})
        self.fulfilled = None
        self.continued = False
        self.fetch_response = fetch_response
        self.fetch_calls = 0

    def fulfill(self, **kwargs):
        self.fulfilled = kwargs

    def continue_(self):
        self.continued = True

    def fetch(self, **kwargs):
        self.fetch_calls += 1
        return self.fetch_response

    def abort(self):
        self.aborted = True


class FakeResponse:
    ok = True
    status = 200
    headers = {"content-type": "application/json"}

    def __init__(self, data):
        self.data = data

    def text(self):
        return json.dumps(self.data)


class WriteSafeGateTests(unittest.TestCase):
    def test_endpoint_pattern_collapses_numeric_and_uuid_ids(self):
        self.assertEqual(endpoint_pattern("DELETE", "https://example.test/api/items/17"),
                         "DELETE example.test/api/items/{id}")
        self.assertEqual(endpoint_pattern("GET", "https://example.test/api/items/123e4567-e89b-12d3-a456-426614174000"),
                         "GET example.test/api/items/{id}")

    def test_url_redaction_removes_credentials_fragments_and_sensitive_queries(self):
        safe = redact_url("https://user:password@example.test/api/token/abcdefghijklmnop?access_token=secret#fragment")
        self.assertNotIn("password", safe)
        self.assertNotIn("secret", safe)
        self.assertNotIn("fragment", safe)
        self.assertIn("[REDACTED]", safe)

    def test_never_rule_precedes_safe_read_and_unknown_write_is_mock(self):
        with tempfile.TemporaryDirectory() as td:
            store = DecisionStore(Path(td) / "decisions.json")
            self.assertEqual(store.classify("GET", "https://x.test/api/reset", "")[:2], ("block", "never rule"))
            self.assertEqual(store.classify("PATCH", "https://x.test/api/items/1", "{}")[0], "mock")

    def test_graphql_query_passes_but_mutation_is_mocked(self):
        with tempfile.TemporaryDirectory() as td:
            store = DecisionStore(Path(td) / "decisions.json")
            self.assertEqual(store.classify(
                "POST", "https://x.test/graphql", '{"query":"query { items { id } }"}',
            )[0], "pass")
            self.assertEqual(store.classify(
                "POST", "https://x.test/graphql", '{"query":"mutation { deleteItem }"}',
            )[0], "mock")

    def test_explicit_safe_request_is_configurable(self):
        with tempfile.TemporaryDirectory() as td:
            store = DecisionStore(Path(td) / "decisions.json")
            pattern = endpoint_pattern("POST", "https://x.test/session/refresh")
            store.data["safe_requests"].append(pattern)
            self.assertEqual(store.classify("POST", "https://x.test/session/refresh")[0], "pass")

    def test_corrupt_decisions_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "decisions.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Cannot safely load endpoint decisions"):
                DecisionStore(path)

    def test_create_marker_is_added_only_to_configured_payload_field(self):
        self.assertEqual(
            json.loads(_tag_create_body('{"name":"widget"}', "name", "run1"))["name"],
            "QA_run1_widget",
        )
        self.assertEqual(
            _tag_create_body("name=widget", "name", "run1"),
            "name=QA_run1_widget",
        )
        graphql = '{"query":"mutation createItem($name:String!) { createItem(name:$name) }",' \
                   '"variables":{"name":"widget"}}'
        tagged_graphql = json.loads(_tag_create_body(graphql, "name", "run1", graphql=True))
        self.assertEqual(tagged_graphql["variables"]["name"], "QA_run1_widget")
        self.assertEqual(_tag_create_body('{"name":"widget"}', "", "run1"), '{"name":"widget"}')

    def test_auth_write_requires_an_explicit_configured_path(self):
        with tempfile.TemporaryDirectory() as td:
            context = FakeContext()
            gate = WriteSafeGate(context, decisions_path=Path(td) / "decisions.json",
                                 report_dir=Path(td) / "reports")
            route = FakeRoute("POST", "https://x.test/login", "user=a&password=b")
            context.handler(route)
            self.assertTrue(route.fulfilled)
            self.assertFalse(route.continued)

            with patch.dict("os.environ", {"WRITE_SAFE_AUTH_PATHS": "/login"}):
                safe_route = FakeRoute("POST", "https://x.test/login?ReturnUrl=%2F", "user=a")
                context.handler(safe_route)
                unsafe_method = FakeRoute("DELETE", "https://x.test/login", "")
                context.handler(unsafe_method)
            self.assertTrue(safe_route.continued)
            self.assertFalse(unsafe_method.continued)
            self.assertTrue(unsafe_method.fulfilled)
            gate.close()

    def test_allowlisted_writes_still_require_account_and_owned_record(self):
        with tempfile.TemporaryDirectory() as td, patch.dict("os.environ", {
            "WRITE_SAFE_ACCOUNT_HEADER": "X-Test-Account",
            "WRITE_SAFE_ACCOUNT_VALUE": "qa-account",
        }):
            context = FakeContext()
            gate = WriteSafeGate(context, decisions_path=Path(td) / "decisions.json",
                                 report_dir=Path(td) / "reports")
            pattern = "PATCH x.test/api/items/{id}"
            gate.decisions.data["allowlist"].extend([pattern, "POST x.test/api/items"])
            headers = {"X-Test-Account": "qa-account"}
            self.assertFalse(gate._allowed_real_write(
                "PATCH", "https://x.test/api/items/7", "{}", headers, pattern,
            ))
            gate.overlay.real_created_ids.add("7")
            self.assertTrue(gate._allowed_real_write(
                "PATCH", "https://x.test/api/items/7", "{}", headers, pattern,
            ))
            self.assertFalse(gate._allowed_real_write(
                "POST", "https://x.test/api/items", '{"note":"prefix QA_item"}', headers,
                "POST x.test/api/items",
            ))
            self.assertTrue(gate._allowed_real_write(
                "POST", "https://x.test/api/items", '{"name":"QA_item"}', headers,
                "POST x.test/api/items",
            ))
            self.assertFalse(gate._allowed_real_write(
                "POST", "https://x.test/api/items", '{"id":"77","name":"QA_item"}',
                headers, "POST x.test/api/items",
            ))
            gate.decisions.data["allowlist"].append("POST x.test/graphql")
            self.assertFalse(gate._allowed_real_write(
                "POST", "https://x.test/graphql",
                '{"query":"mutation updateItem($id: ID!, $name: String!) { updateItem(id:$id,name:$name) }",'
                '"variables":{"id":"7","name":"QA_item"}}', headers,
                "POST x.test/graphql",
            ))
            gate.close()

    def test_mock_create_and_delete_are_visible_in_get_overlay(self):
        overlay = MockOverlay()
        request = SimpleNamespace(headers={})
        route = FakeRoute("POST", "https://x.test/api/items", '{"name":"one"}')
        status, record = overlay.mutate("POST", route.request.url, route.request.post_data, request, route)
        self.assertEqual(status, 201)
        self.assertEqual(len(overlay.merge_get("https://x.test/api/items", [])[1]), 1)
        rid = str(record["id"])
        overlay.mutate("DELETE", f"https://x.test/api/items/{rid}", "", request,
                       FakeRoute("DELETE", f"https://x.test/api/items/{rid}"))
        self.assertEqual(overlay.merge_get("https://x.test/api/items", [record])[1], [])

    def test_mock_patch_is_visible_in_collection_and_single_record_reads(self):
        overlay = MockOverlay()
        request = SimpleNamespace(headers={})
        route = FakeRoute("PATCH", "https://x.test/api/items/17", '{"name":"updated"}',
                          fetch_response=FakeResponse({"id": 17, "name": "old"}))
        status, patched = overlay.mutate("PATCH", route.request.url, route.request.post_data,
                                         request, route)
        self.assertEqual(status, 200)
        self.assertEqual(route.fetch_calls, 1)
        self.assertEqual(patched["name"], "updated")
        self.assertEqual(overlay.merge_get("https://x.test/api/items/17", None)[1]["name"], "updated")
        collection = overlay.merge_get("https://x.test/api/items", [{"id": 17, "name": "old"}])[1]
        self.assertEqual(collection[0]["name"], "updated")

    def test_openapi_create_example_seeds_mock_shape(self):
        overlay = MockOverlay()
        overlay.seed_openapi({"paths": {"/api/items": {"post": {"responses": {
            "201": {"content": {"application/json": {"schema": {"type": "object", "properties": {
                "id": {"type": "string"}, "name": {"type": "string", "example": "sample"},
            }}}}}
        }}}}}, "https://x.test")
        route = FakeRoute("POST", "https://x.test/api/items", '{"name":"created"}')
        status, data = overlay.mutate("POST", route.request.url, route.request.post_data,
                                     SimpleNamespace(headers={}), route)
        self.assertEqual(status, 201)
        self.assertEqual(data["name"], "created")
        self.assertTrue(str(data["id"]).startswith("QA_"))

    def test_route_mocks_write_and_queues_it_but_passes_safe_read(self):
        with tempfile.TemporaryDirectory() as td:
            context = FakeContext()
            gate = WriteSafeGate(context, decisions_path=Path(td) / "decisions.json",
                                 report_dir=Path(td) / "reports")
            write = FakeRoute("POST", "https://x.test/api/items", '{"name":"one"}')
            context.handler(write)
            self.assertEqual(write.fulfilled["status"], 201)
            self.assertEqual(write.fetch_calls, 0)
            self.assertFalse(write.continued)
            self.assertIn("POST x.test/api/items", gate.decisions.data["queue"])
            read = FakeRoute("GET", "https://x.test/api/items")
            read.fetch_response = FakeResponse([])
            context.handler(read)
            self.assertEqual(json.loads(read.fulfilled["body"])[0]["name"], "one")
            self.assertEqual([event["decision"] for event in gate.events], ["mock", "mock"])
            self.assertTrue(gate.events[0]["queued_for_review"])
            gate.close()
            self.assertTrue(list((Path(td) / "reports").glob("report-*.md")))

    def test_checkpoint_can_resume_from_last_successful_path(self):
        with tempfile.TemporaryDirectory() as td:
            report_dir = Path(td) / "reports"
            context = FakeContext()
            gate = WriteSafeGate(context, decisions_path=Path(td) / "decisions.json",
                                 report_dir=report_dir)
            action = SimpleNamespace(kind="click", role="button", name="Next", value="", dup_index=0)
            secret_action = SimpleNamespace(kind="fill", role="textbox", name="Password",
                                            value="sensitive-value", dup_index=0)
            gate.checkpoint("Click Next", "https://x.test/start", "ok",
                            start_url="https://x.test/start", resume_path=[action, secret_action])
            self.assertNotIn("sensitive-value", (report_dir / "resume.json").read_text(encoding="utf-8"))
            for _ in range(3):
                gate.checkpoint("Click Slow", "https://x.test/start", "timeout",
                                start_url="https://x.test/start", resume_path=[action, secret_action])
            self.assertTrue(gate.should_skip("https://x.test/start", "Click Slow"))
            gate.close()

            resumed = WriteSafeGate(FakeContext(), decisions_path=Path(td) / "decisions.json",
                                    report_dir=report_dir, base_url="https://x.test")
            self.assertEqual(resumed.resume_path("https://x.test/start")[0]["name"], "Next")
            self.assertEqual(resumed.resume_path("https://x.test/start")[1]["value"], "")
            resumed.close()

    def test_declined_action_is_reported_without_counting_as_failure(self):
        with tempfile.TemporaryDirectory() as td:
            report_dir = Path(td) / "reports"
            gate = WriteSafeGate(FakeContext(), decisions_path=Path(td) / "decisions.json",
                                 report_dir=report_dir)
            gate.checkpoint("Click Save", "https://x.test/start", "declined")
            self.assertFalse(gate.should_skip("https://x.test/start", "Click Save"))
            gate.close()
            report = json.loads(next(report_dir.glob("report-*.json")).read_text(encoding="utf-8"))
            self.assertEqual(report["skiplist"][0]["outcome"], "declined")

    def test_review_is_capped_and_defer_keeps_items_queued(self):
        with tempfile.TemporaryDirectory() as td:
            decisions_path = Path(td) / "decisions.json"
            store = DecisionStore(decisions_path)
            for index in range(11):
                pattern = f"POST x.test/api/items/{index}"
                store.queue(pattern, "POST", f"https://x.test/api/items/{index}", "Click Save")
            output = io.StringIO()
            with patch.dict("os.environ", {"WRITE_SAFE_REPORT_DIR": str(Path(td) / "reports")}), \
                 patch.object(review_module, "_read_key", return_value="d"), \
                 redirect_stdout(output):
                review_module.review(decisions_path, timeout=2)
            self.assertEqual(output.getvalue().count("Pattern:"), 10)
            reviewed = DecisionStore(decisions_path)
            self.assertEqual(sum(q["status"] == "pending" for q in reviewed.data["queue"].values()), 11)


if __name__ == "__main__":
    unittest.main()

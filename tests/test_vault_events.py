import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

from capture_app.storage.vault import VaultWriter


class SessionApiEventTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault = VaultWriter(Path(self.temp_dir.name))

    def tearDown(self):
        self.vault.conn.close()
        self.temp_dir.cleanup()

    def _record(self, **overrides):
        record = {
            "method": "POST",
            "url": "https://shop.example.test/search?term=book",
            "status": 200,
            "request_headers": {"Content-Type": "application/x-www-form-urlencoded"},
            "response_headers": {"Content-Type": "application/json"},
            "payload": "term=book",
            "response_body": '{"result":"book"}',
            "entry_point": "Clicked `button.search` (\"Search\")",
            "correlation_status": "heuristic",
            "action_id": "session-action-1",
            "ts": 1000.0,
        }
        record.update(overrides)
        return record

    def test_repeated_endpoint_occurrences_keep_their_own_evidence(self):
        self.vault.start_session("session-a", "https://shop.example.test")
        self.vault.ingest(self._record(event_seq=4), "session-a")
        self.vault.record_step("session-a", {
            "step_type": "tap", "action_id": "session-action-1",
            "selector": "button.search", "label": "Search", "ts": 900.0,
        })
        self.vault.ingest(self._record(
            url="https://shop.example.test/search?term=lamp",
            status=404,
            request_headers={"Content-Type": "application/x-www-form-urlencoded", "X-Request": "second"},
            response_headers={"Content-Type": "application/json", "X-Response": "second"},
            payload="term=lamp",
            response_body='{"result":"lamp"}',
            entry_point="Auto / page load on https://shop.example.test",
            correlation_status="unmatched",
            action_id=None,
            event_seq=9,
            ts=2000.0,
        ), "session-a")

        events = self.vault.conn.execute(
            """SELECT seq, status, request_url, request_headers_json, request_body,
                      response_headers_json, response_body, entry_point, correlation_status, action_id
               FROM session_api_events WHERE session_id=? ORDER BY seq""",
            ("session-a",),
        ).fetchall()

        self.assertEqual([row[0] for row in events], [4, 9])
        self.assertEqual([row[1] for row in events], [200, 404])
        self.assertIn("term=book", events[0][2])
        self.assertIn("term=lamp", events[1][2])
        self.assertEqual(events[0][4], "term=book")
        self.assertEqual(events[1][4], "term=lamp")
        self.assertEqual(json.loads(events[1][3])["X-Request"], "second")
        self.assertEqual(json.loads(events[1][5])["X-Response"], "second")
        self.assertEqual(json.loads(events[1][6]), {"result": "lamp"})
        self.assertEqual(events[0][7], "Clicked `button.search` (\"Search\")")
        self.assertEqual([row[8] for row in events], ["heuristic", "unmatched"])
        self.assertEqual(events[0][9], "session-action-1")
        self.assertIsNone(events[1][9])
        step_action_id = self.vault.conn.execute(
            "SELECT action_id FROM steps WHERE session_id=?", ("session-a",)
        ).fetchone()[0]
        self.assertEqual(step_action_id, events[0][9])

    def test_sensitive_values_are_redacted_before_event_and_aggregate_storage(self):
        self.vault.start_session("session-private", "https://shop.example.test")
        self.vault.ingest(self._record(
            url="https://shop.example.test/search?token=query-secret&term=book",
            request_headers={"Authorization": "auth-secret", "Cookie": "cookie-secret"},
            response_headers={"Set-Cookie": "response-cookie-secret"},
            payload='{"email":"person@example.test","password":"body-secret","term":"book"}',
            response_body='{"token":"response-secret","result":"ok"}',
            entry_point="Clicked Search for person@example.test",
        ), "session-private")

        event = self.vault.conn.execute(
            """SELECT request_url, request_headers_json, request_body,
                      response_headers_json, response_body, entry_point
               FROM session_api_events WHERE session_id=?""",
            ("session-private",),
        ).fetchone()
        aggregate = self.vault.conn.execute(
            "SELECT full_url, headers_json, payload_json, response_json FROM apis"
        ).fetchone()
        stored = " ".join(str(value) for value in (*event, *aggregate))

        self.assertNotIn("query-secret", unquote(event[0]))
        self.assertNotIn("auth-secret", stored)
        self.assertNotIn("cookie-secret", stored)
        self.assertNotIn("person@example.test", stored)
        self.assertNotIn("body-secret", stored)
        self.assertNotIn("response-secret", stored)
        self.assertNotIn("person@example.test", event[5])
        self.assertEqual(json.loads(event[2])["email"], "[REDACTED]")
        self.assertEqual(json.loads(event[2])["password"], "[REDACTED]")

    def test_api_note_contains_postman_importable_curl_without_saved_credentials(self):
        self.vault.ingest(self._record(
            url="https://shop.example.test/add-to-cart",
            request_headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "Cookie": "session=cookie-secret",
                "Authorization": "Bearer auth-secret",
                "Content-Length": "12",
                "Host": "shop.example.test",
            },
            payload="quantity=1",
        ))

        note = Path(self.temp_dir.name) / "APIs" / "shop.example.test" / "POST_add-to-cart.md"
        content = note.read_text(encoding="utf-8")

        self.assertIn("## cURL (Postman)", content)
        self.assertIn("Import → Raw text", content)
        self.assertIn("curl --location --request POST", content)
        self.assertIn("--url 'https://shop.example.test/add-to-cart'", content)
        self.assertIn("--header 'Content-Type: application/x-www-form-urlencoded'", content)
        self.assertIn("--header 'Cookie: [REDACTED]'", content)
        self.assertIn("--header 'Authorization: [REDACTED]'", content)
        self.assertIn("--data-raw 'quantity=1'", content)
        self.assertNotIn("$env:", content)
        self.assertNotIn("cookie-secret", content)
        self.assertNotIn("auth-secret", content)
        self.assertNotIn("--header 'Content-Length:", content)
        self.assertNotIn("--header 'Host:", content)

    def test_session_start_url_redacts_userinfo_and_sensitive_query(self):
        self.vault.start_session(
            "session-url-secret",
            "https://user:password@shop.example.test/start?token=query-secret&next=%2Fhome",
        )

        stored_url = self.vault.conn.execute(
            "SELECT start_url FROM sessions WHERE session_id=?", ("session-url-secret",)
        ).fetchone()[0]

        self.assertNotIn("user", stored_url)
        self.assertNotIn("password", stored_url)
        self.assertNotIn("query-secret", unquote(stored_url))
        self.assertIn("next=%2Fhome", stored_url)

    def test_sensitive_ui_input_becomes_fixture_variable_for_replay(self):
        self.vault.start_session("session-ui", "https://shop.example.test")
        self.vault.record_step("session-ui", {
            "step_type": "input",
            "action_id": "session-ui-action-1",
            "selector": "#Email",
            "label": "Email address",
            "value": "person@example.test",
            "input_type": "email",
            "page_url": "https://shop.example.test/login",
            "ts": 1000.0,
        })

        stored = self.vault.conn.execute(
            "SELECT value FROM steps WHERE session_id=?", ("session-ui",)
        ).fetchone()[0]
        lines, human_lines, env_vars = self.vault._render_step_lines([(
            "input", "#Email", "Email address", stored, "email",
            "https://shop.example.test/login", None, None, None, None, None,
            None, None, None, 1000.0,
        )])

        self.assertEqual(stored, "${TEST_EMAIL_ADDRESS}")
        self.assertNotIn("person@example.test", stored)
        self.assertIn("${TEST_EMAIL_ADDRESS}", lines[0])
        self.assertEqual(env_vars, {"TEST_EMAIL_ADDRESS": "<fill in>"})
        self.assertIn("TEST_EMAIL_ADDRESS", human_lines[0])

    def test_maestro_export_ignores_redacted_email_labels_for_taps(self):
        lines, human_lines, env_vars = self.vault._render_step_lines([(
            "tap", "#gift-email-input", "[REDACTED:email]", None, None, "https://shop.example.test/checkout",
            "input", "email", None, None, None, "Email address", None, None, 1000.0,
        )])

        rendered = "\n".join(lines + human_lines)
        self.assertNotIn("[REDACTED:email]", rendered)
        self.assertNotIn("\\[REDACTED:email\\]", rendered)
        self.assertIn("Email address", rendered)
        self.assertEqual(env_vars, {})

    def test_assertion_builder_skips_counter_and_redacted_values(self):
        body, human = self.vault._build_assertion_for_event(
            "POST_add_to_cart",
            200,
            '{"count": "(6)", "message": "<b>Added</b> to cart", "email": "[REDACTED:email]"}',
        )

        rendered = "\n".join(body + human)
        self.assertNotIn("(6)", rendered)
        self.assertNotIn("[REDACTED", rendered)
        self.assertIn("Added", rendered)
        self.assertNotIn("status `200` is shown", rendered)

    def test_scenario_annotation_round_trip_and_constraint_sources(self):
        annotation = {
            "schema_version": 1,
            "scenario_id": "checkout-guest-1",
            "scenario_name": "Guest checkout",
            "intent": "Place an order as a guest",
            "actor_role": "guest",
            "environment": "local-test",
            "fixture_alias": "CHECKOUT_GUEST",
            "starting_state": "Cart contains one in-stock product",
            "selected_variants": {"payment": "Payments.CashOnDelivery"},
            "field_constraints": [
                {"field": "city", "rule": "required", "source": "curated"},
                {"field": "payment", "rule": "CashOnDelivery", "source": "observed"},
            ],
            "expected_assertions": ["Captured confirmation response is successful"],
            "side_effects": ["Creates an order"],
            "replay_risk": "mutating",
        }
        self.vault.start_session("session-annotation", "https://shop.example.test")

        saved = self.vault.save_scenario_annotation("session-annotation", annotation)
        loaded = self.vault.get_scenario_annotation("session-annotation")

        self.assertEqual(saved, annotation)
        self.assertEqual(loaded, annotation)
        self.assertEqual(
            {item["source"] for item in loaded["field_constraints"]},
            {"curated", "observed"},
        )

    def test_scenario_annotation_rejects_unknown_constraint_source(self):
        with self.assertRaisesRegex(ValueError, "source must be one of"):
            self.vault.save_scenario_annotation("session-invalid", {
                "schema_version": 1,
                "field_constraints": [
                    {"field": "quantity", "rule": "positive", "source": "inferred-by-model"},
                ],
            })

    def test_scenario_annotation_redacts_email_strings(self):
        saved = self.vault.save_scenario_annotation("session-redact", {
            "schema_version": 1,
            "scenario_name": "Checkout for person@example.test",
            "selected_variants": {"email": "person@example.test"},
        })

        self.assertNotIn("person@example.test", json.dumps(saved))

    def test_maestro_export_links_flow_to_event_and_scenario(self):
        session_id = "session-flow-provenance"
        self.vault.start_session(session_id, "https://shop.example.test")
        self.vault.ingest(self._record(
            event_seq=7, action_id="session-flow-provenance-action-1",
        ), session_id)
        self.vault.save_scenario_annotation(session_id, {
            "schema_version": 1,
            "scenario_id": "search-basic",
            "scenario_name": "Basic search",
        })

        flows = self.vault.export_maestro_flows(session_id)
        yaml_text = Path(flows[0]["yaml"]).read_text(encoding="utf-8")
        flow_note = Path(flows[0]["md"]).read_text(encoding="utf-8")
        session_note = Path(flows[0]["session_md"]).read_text(encoding="utf-8")

        self.assertEqual(flows[0]["event_id"], 1)
        self.assertEqual(flows[0]["event_seq"], 7)
        self.assertEqual(flows[0]["action_id"], "session-flow-provenance-action-1")
        self.assertEqual(flows[0]["scenario_id"], "search-basic")
        self.assertIn("# event_id: 1", yaml_text)
        self.assertIn("# event_seq: 7", yaml_text)
        self.assertIn("# action_id: session-flow-provenance-action-1", yaml_text)
        self.assertIn("# scenario_id: search-basic", yaml_text)
        self.assertIn("event_id: 1", flow_note)
        self.assertIn("event_seq: 7", flow_note)
        self.assertIn("action_id: session-flow-provenance-action-1", flow_note)
        self.assertIn("scenario_id: search-basic", flow_note)
        self.assertIn("event 1 / POST_search", session_note)
        self.assertIn("scenario_id: search-basic", session_note)

    def test_existing_event_schema_is_migrated_without_losing_rows(self):
        self.vault.conn.close()
        db_path = Path(self.temp_dir.name) / ".capture_index" / "index.sqlite"
        conn = sqlite3.connect(db_path)
        conn.execute("DROP TABLE session_api_events")
        conn.execute(
            """CREATE TABLE session_api_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                ref_stem TEXT NOT NULL,
                domain TEXT,
                method TEXT,
                status INTEGER,
                response_body TEXT,
                ts REAL
            )"""
        )
        conn.execute(
            """INSERT INTO session_api_events
               (session_id, seq, ref_stem, domain, method, status, response_body, ts)
               VALUES ('legacy', 1, 'GET_search', 'shop.example.test', 'GET', 200, 'ok', 1)"""
        )
        conn.commit()
        conn.close()

        migrated = VaultWriter(Path(self.temp_dir.name))
        columns = {row[1] for row in migrated.conn.execute("PRAGMA table_info(session_api_events)")}
        legacy_row = migrated.conn.execute(
            "SELECT session_id, seq, ref_stem, response_body FROM session_api_events"
        ).fetchone()

        self.assertTrue({
            "request_url", "request_headers_json", "request_body",
            "response_headers_json", "entry_point", "correlation_status",
        }.issubset(columns))
        self.assertEqual(legacy_row, ("legacy", 1, "GET_search", "ok"))
        migrated.conn.close()


if __name__ == "__main__":
    unittest.main()
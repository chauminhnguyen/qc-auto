import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from capture_app.reporting.visualizer import GraphVisualizer
from capture_app.storage.vault import VaultWriter
from capture_app.workflows import enrich


class VaultRedactionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault = VaultWriter(Path(self.temp_dir.name))

    def tearDown(self):
        self.vault.conn.close()
        self.temp_dir.cleanup()

    def _ingest_api(self, url="https://shop.example.test/items"):
        self.vault.ingest({
            "method": "GET",
            "url": url,
            "status": 200,
            "request_headers": {},
            "response_headers": {},
            "payload": "",
            "response_body": "{}",
            "entry_point": "test",
        })
        return self.vault.conn.execute("SELECT id FROM apis").fetchone()[0]

    def test_url_userinfo_is_not_saved_as_domain(self):
        self._ingest_api("https://capture-user:capture-password@shop.example.test/items")

        domain, full_url = self.vault.conn.execute(
            "SELECT domain, full_url FROM apis"
        ).fetchone()

        self.assertEqual(domain, "shop.example.test")
        self.assertNotIn("capture-user", full_url)
        self.assertNotIn("capture-password", full_url)

    def test_variant_responses_are_redacted_before_storage(self):
        api_id = self._ingest_api()
        private_email = "private.person@example.test"
        private_token = "sensitive-response-token"
        private_cookie = "session=sensitive-cookie-value"

        self.vault.save_variant_run(
            api_id,
            {"query": "safe"},
            200,
            {"Set-Cookie": private_cookie},
            json.dumps({"email": private_email, "csrf_token": private_token}),
        )

        headers_json, body = self.vault.conn.execute(
            "SELECT response_headers_json, response_body FROM variant_runs"
        ).fetchone()
        persisted = headers_json + body
        for value in (private_email, private_token, private_cookie):
            self.assertNotIn(value, persisted)

        values_json = self.vault.conn.execute(
            "SELECT example_values_json FROM params"
        ).fetchone()[0]
        for value in (private_email, private_token):
            self.assertNotIn(value, values_json)


class ExternalAndHtmlBoundaryTests(unittest.TestCase):
    def test_enrichment_redacts_response_before_prompt_data_is_returned(self):
        private_email = "private.person@example.test"
        private_token = "sensitive-response-token"
        response = SimpleNamespace(
            status_code=200,
            headers={"Set-Cookie": "session=sensitive-cookie-value"},
            text=json.dumps({"email": private_email, "token": private_token}),
        )
        client = MagicMock()
        client.__enter__.return_value.request.return_value = response

        with patch.object(enrich.httpx, "Client", return_value=client), patch.object(
            enrich.time, "sleep"
        ):
            results = enrich.run_variants(
                {"headers_json": "{}", "full_url": "https://shop.example.test", "method": "GET"},
                [{"query": "safe"}],
            )

        _, user_prompt = enrich.build_batch_definition_prompt([
            ({"method": "GET", "full_url": "https://shop.example.test", "resource_key": "shop:item"},
             [{"name": "query"}], results)
        ])
        for value in (private_email, private_token, "sensitive-cookie-value"):
            self.assertNotIn(value, user_prompt)

    def test_visualizer_escapes_untrusted_graph_data(self):
        payload = "</script><img src=x onerror=alert(1)>"
        html = GraphVisualizer._build_html(
            [{
                "id": payload,
                "label": payload,
                "description": payload,
                "conditions": [payload],
                "color": "#3498db",
                "is_composite": False,
                "api_note_path": "",
                "vault_path": "",
            }],
            [],
        )

        self.assertNotIn(payload, html)
        self.assertIn(r"\u003c/script\u003e", html)
        self.assertIn("escapeHtml(d.label)", html)
        self.assertIn("escapeHtml(d.description)", html)


if __name__ == "__main__":
    unittest.main()
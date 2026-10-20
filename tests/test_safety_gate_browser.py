import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

from capture_app.safety_gate.gate import WriteSafeGate


class SpyHandler(BaseHTTPRequestHandler):
    writes = 0

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/":
            body = (b'<form action="/api/items" method="post">'
                    b'<input name="name" value="from-form">'
                    b'<button type="submit">Save</button></form>')
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        else:
            body = b"[]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).writes += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"real":true}')


class WriteSafeBrowserIntegrationTests(unittest.TestCase):
    def test_form_writes_are_mocked_and_never_reach_local_server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), SpyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        SpyHandler.writes = 0
        with tempfile.TemporaryDirectory() as td:
            decisions = Path(td) / "decisions.json"
            reports = Path(td) / "reports"
            playwright = sync_playwright().start()
            browser = None
            try:
                try:
                    browser = playwright.chromium.launch(channel="msedge", headless=True)
                except Exception:
                    try:
                        browser = playwright.chromium.launch(headless=True)
                    except Exception as exc:
                        self.skipTest(f"No Playwright browser installed: {type(exc).__name__}")
                context = browser.new_context(service_workers="block")
                gate = WriteSafeGate(context, decisions_path=decisions, report_dir=reports)
                page = context.new_page()
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                with page.expect_response("**/api/items") as response_info:
                    page.get_by_role("button", name="Save").click()
                self.assertEqual(response_info.value.status, 201)
                self.assertEqual(SpyHandler.writes, 0)

                collection = page.evaluate("async () => (await fetch('/api/items')).json()")
                self.assertEqual(collection[0]["name"], "from-form")

                blocked = page.evaluate("async () => (await fetch('/api/reset', {method: 'POST'})).status")
                self.assertEqual(blocked, 200)
                self.assertEqual(SpyHandler.writes, 0)
                gate.close()
                context.close()
            finally:
                if browser:
                    browser.close()
                playwright.stop()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

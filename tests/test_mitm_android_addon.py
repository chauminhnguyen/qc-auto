import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

try:
    from capture_app.workflows import mitm_android_addon
except ImportError:
    mitm_android_addon = None


@unittest.skipIf(mitm_android_addon is None, "mitmproxy is an optional Android capture dependency")
class MitmAndroidDiagnosticTests(unittest.TestCase):
    def test_client_connection_counter_is_scoped_to_wireless_phone_ip(self):
        addon = mitm_android_addon.AndroidCaptureAddon()
        options = SimpleNamespace(android_client_ip="192.168.1.184", android_allowed_hosts="[]")
        with patch.object(mitm_android_addon.ctx, "options", options, create=True), \
             patch.object(addon, "_send_diagnostic") as send:
            addon.client_connected(SimpleNamespace(peername=("192.168.1.184", 42000)))
            addon.client_connected(SimpleNamespace(peername=("127.0.0.1", 50000)))
            addon.client_connected(SimpleNamespace(peername=("192.168.1.12", 42001)))
        self.assertEqual(send.call_args_list, [
            call("client_connections"),
            call("client_connections_from_device"),
            call("client_connections"),
            call("other_client_connections"),
        ])

    def test_tls_failure_records_host_only_and_never_records_request_values(self):
        addon = mitm_android_addon.AndroidCaptureAddon()
        options = SimpleNamespace(android_client_ip="192.168.1.184", android_allowed_hosts="[]")
        data = SimpleNamespace(conn=SimpleNamespace(
            peername=("192.168.1.184", 42000), sni="api.example.test",
        ))
        with patch.object(mitm_android_addon.ctx, "options", options, create=True), \
             patch.object(addon, "_send_diagnostic") as diagnostic, \
             patch.object(addon, "_send_failure") as failure:
            addon.tls_failed_client(data)
        diagnostic.assert_called_once_with("client_tls_failures", "api.example.test")
        failure.assert_called_once_with("api.example.test", "tls")


if __name__ == "__main__":
    unittest.main()

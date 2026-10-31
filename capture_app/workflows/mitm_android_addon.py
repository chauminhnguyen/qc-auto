"""mitmdump addon that streams redacted Android traffic into the run collector."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from mitmproxy import ctx, http

from capture_app.workflows.android_capture_support import host_allowed, sanitize_event


MAX_CAPTURE_BODY = 2_000_000


def _text(message) -> str:
    try:
        value = message.get_text(strict=False)
    except Exception:
        value = ""
    return value[:MAX_CAPTURE_BODY]


class AndroidCaptureAddon:
    def load(self, loader):
        loader.add_option("android_collector_url", str, "", "Local Android capture collector URL")
        loader.add_option("android_allowed_hosts", str, "[]", "JSON host allowlist for Android capture")
        loader.add_option("android_client_ip", str, "", "Expected Android client IP for capture diagnostics")

    @staticmethod
    def _allowed(host: str) -> bool:
        try:
            allowed = tuple(json.loads(ctx.options.android_allowed_hosts or "[]"))
        except (ValueError, TypeError):
            allowed = ()
        return not allowed or host_allowed(host, allowed)

    @staticmethod
    def _client_scope(client) -> str | None:
        expected = str(getattr(ctx.options, "android_client_ip", "") or "").strip()
        peer = getattr(client, "peername", None)
        if not peer:
            return None
        actual = str(peer[0])
        if actual in {"127.0.0.1", "::1"}:
            return None
        if expected:
            return "device" if actual == expected else "other"
        return "unfiltered"

    @classmethod
    def _client_allowed(cls, client) -> bool:
        return cls._client_scope(client) in {"device", "unfiltered"}

    @staticmethod
    def _flow_host(flow) -> str:
        request = getattr(flow, "request", None)
        if request is not None:
            return str(getattr(request, "pretty_host", "") or "").casefold().rstrip(".")
        server = getattr(flow, "server_conn", None)
        address = getattr(server, "address", None)
        return str(address[0] if address else "").casefold().rstrip(".")

    def client_connected(self, client):
        scope = self._client_scope(client)
        if scope:
            self._send_diagnostic("client_connections")
            if scope == "device":
                self._send_diagnostic("client_connections_from_device")
            elif scope == "other":
                self._send_diagnostic("other_client_connections")

    def client_disconnected(self, client):
        if self._client_scope(client) in {"device", "unfiltered"}:
            self._send_diagnostic("client_disconnects")

    def http_connect(self, flow: http.HTTPFlow):
        if self._client_allowed(flow.client_conn):
            host = self._flow_host(flow)
            if self._allowed(host):
                self._send_diagnostic("proxy_connect_requests", host)

    def server_connected(self, data):
        if self._client_allowed(data.client):
            host = str((getattr(data.server, "address", None) or ("",))[0]).casefold().rstrip(".")
            if self._allowed(host):
                self._send_diagnostic("upstream_connections", host)

    def server_connect_error(self, data):
        if self._client_allowed(data.client):
            host = str((getattr(data.server, "address", None) or ("",))[0]).casefold().rstrip(".")
            if self._allowed(host):
                self._send_diagnostic("upstream_connection_failures", host)
                self._send_failure(host, "connection")

    def tls_established_client(self, data):
        if self._client_allowed(data.conn):
            host = str(getattr(data.conn, "sni", "") or "").casefold().rstrip(".")
            if self._allowed(host):
                self._send_diagnostic("client_tls_successes", host)

    def tls_failed_client(self, data):
        if self._client_allowed(data.conn):
            host = str(getattr(data.conn, "sni", "") or "").casefold().rstrip(".")
            if self._allowed(host):
                self._send_diagnostic("client_tls_failures", host)
                self._send_failure(host, "tls")

    def tls_established_server(self, data):
        if self._client_allowed(data.context.client):
            host = str(getattr(data.conn, "sni", "") or "").casefold().rstrip(".")
            if self._allowed(host):
                self._send_diagnostic("server_tls_successes", host)

    def tls_failed_server(self, data):
        if self._client_allowed(data.context.client):
            host = str(getattr(data.conn, "sni", "") or "").casefold().rstrip(".")
            if self._allowed(host):
                self._send_diagnostic("server_tls_failures", host)
                self._send_failure(host, "tls")

    def tcp_start(self, flow):
        if self._client_allowed(flow.client_conn):
            host = self._flow_host(flow)
            if self._allowed(host):
                self._send_diagnostic("tcp_flows", host)

    def udp_start(self, flow):
        if self._client_allowed(flow.client_conn):
            host = self._flow_host(flow)
            if self._allowed(host):
                self._send_diagnostic("udp_flows", host)

    def request(self, flow: http.HTTPFlow):
        flow.metadata["android_capture_started"] = datetime.now(timezone.utc).timestamp()
        if self._client_allowed(flow.client_conn):
            host = self._flow_host(flow)
            if self._allowed(host):
                self._send_diagnostic("http_requests", host)

    def response(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host.lower()
        if self._client_allowed(flow.client_conn) and self._allowed(host):
            self._send_diagnostic("http_responses", host)
        try:
            allowed = tuple(json.loads(ctx.options.android_allowed_hosts or "[]"))
        except (ValueError, TypeError):
            allowed = ()
        if allowed and not host_allowed(host, allowed):
            return
        started = float(flow.metadata.get("android_capture_started", flow.request.timestamp_start or 0))
        finished = float(flow.response.timestamp_end or datetime.now(timezone.utc).timestamp())
        response_type = str(flow.response.headers.get("content-type", "")).split(";", 1)[0].strip().casefold()
        raw_response = flow.response.raw_content or b""
        response_body = raw_response if response_type.startswith("image/") or response_type in {
            "application/pdf", "application/x-pdf"
        } else _text(flow.response)
        event = sanitize_event({
            "method": flow.request.method,
            "url": flow.request.pretty_url,
            "status": flow.response.status_code,
            "request_headers": dict(flow.request.headers.items()),
            "payload": _text(flow.request),
            "response_headers": dict(flow.response.headers.items()),
            "response_body": response_body,
            "ts": started,
            "response_ts": finished,
            "duration_ms": max(0, round((finished - started) * 1000, 3)),
        })
        self._send(event)

    def error(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host.lower()
        if self._client_allowed(flow.client_conn) and self._allowed(host):
            self._send_diagnostic("http_errors", host)
        try:
            allowed = tuple(json.loads(ctx.options.android_allowed_hosts or "[]"))
        except (ValueError, TypeError):
            allowed = ()
        if allowed and not host_allowed(host, allowed):
            return
        message = str(getattr(flow.error, "msg", "")).casefold()
        kind = "tls" if any(term in message for term in ("tls", "ssl", "certificate", "handshake")) else "connection"
        self._send_failure(host, kind)
        started = float(flow.metadata.get("android_capture_started", flow.request.timestamp_start or 0))
        self._send(sanitize_event({
            "method": flow.request.method,
            "url": flow.request.pretty_url,
            "request_headers": dict(flow.request.headers.items()),
            "payload": _text(flow.request), "ts": started,
            "response_ts": datetime.now(timezone.utc).timestamp(),
        }))

    @staticmethod
    def _send(event: dict):
        target = str(ctx.options.android_collector_url or "").rstrip("/") + "/event"
        if not target or target == "/event":
            return
        request = urllib.request.Request(
            target, data=json.dumps(event).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2):
                pass
        except (OSError, urllib.error.URLError):
            ctx.log.warn("Android capture collector did not accept a sanitized network event")

    @staticmethod
    def _send_failure(host: str, kind: str):
        target = str(ctx.options.android_collector_url or "").rstrip("/") + "/failure"
        if not target or target == "/failure":
            return
        payload = json.dumps({"host": host, "kind": kind}).encode("utf-8")
        request = urllib.request.Request(
            target, data=payload, headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2):
                pass
        except (OSError, urllib.error.URLError):
            ctx.log.warn("Android capture collector did not accept a connection failure count")

    @staticmethod
    def _send_diagnostic(kind: str, host: str = ""):
        target = str(ctx.options.android_collector_url or "").rstrip("/") + "/diagnostic"
        if not target or target == "/diagnostic":
            return
        payload = json.dumps({"kind": kind, "host": host}).encode("utf-8")
        request = urllib.request.Request(
            target, data=payload, headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2):
                pass
        except (OSError, urllib.error.URLError):
            ctx.log.warn("Android capture collector did not accept a proxy diagnostic")


addons = [AndroidCaptureAddon()]


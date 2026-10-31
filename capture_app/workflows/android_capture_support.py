"""Shared profile, network evidence, marker, and redaction helpers for Android capture."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import xml.etree.ElementTree as ET
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from capture_app.storage.vault import (
    _EMAIL_ADDRESS_RE,
    _is_sensitive_field,
    _redact_body,
    _redact_url,
)

_ANDROID_PRIVATE_FIELD = re.compile(
    r"(?i)\b(?:otp|one[_ -]?time[_ -]?(?:code|password|passcode)|verification[_ -]?code|"
    r"pin|passcode|totp|mfa|card[_ -]?number|account[_ -]?number)\b"
)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
    r"(?<!\w)\+?\d[\d .()/-]{7,}\d(?!\w)|\b\d{4,}\b)"
)


def _android_sensitive(name: Any) -> bool:
    return _is_sensitive_field(name) or bool(_ANDROID_PRIVATE_FIELD.search(str(name)))


def _redact_android_url(url: str) -> str:
    safe = _redact_url(url)
    parts = urlsplit(safe)
    query = [(key, "[REDACTED]" if _android_sensitive(key) else value)
             for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query) if query else parts.query, parts.fragment))


def _redact_android_headers(headers: Any) -> dict:
    if not isinstance(headers, dict):
        return {}
    return {str(key): "[REDACTED]" if _android_sensitive(key) else str(value)
            for key, value in headers.items()}


def _redact_android_body(value: Any) -> str:
    if isinstance(value, (dict, list)):
        def clean(item):
            if isinstance(item, dict):
                return {key: "[REDACTED]" if _android_sensitive(key) else clean(child)
                        for key, child in item.items()}
            if isinstance(item, list):
                return [clean(child) for child in item]
            if isinstance(item, str):
                return redact_marker_text(item)
            return item
        return _redact_body(json.dumps(clean(value), ensure_ascii=False))
    text = str(value or "")
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        fields = parse_qsl(text, keep_blank_values=True)
        if fields and "=" in text:
            text = urlencode([(key, "[REDACTED]" if _android_sensitive(key) else val)
                              for key, val in fields])
        else:
            text = re.sub(
                r"(?i)(otp|pin|passcode|verification[_ -]?code)([\"'=:\s]+)([^\"'<>,\s]+)",
                r"\1\2[REDACTED]", text,
            )
        return _redact_body(text)
    return _redact_android_body(parsed)


@dataclass(frozen=True)
class AndroidAppProfile:
    name: str
    app_id: str
    flows: Path
    expected_text: str | None = None
    network_source: str = "proxy"
    allow_prefix_replay: bool = False
    allowed_hosts: tuple[str, ...] = ()
    settle_ms: int = 750
    proxy_host: str = "0.0.0.0"
    proxy_port: int = 8080
    network_sources: tuple[str, ...] = ("proxy",)
    ui_snapshots: bool = False
    maestro_permitted: bool = True
    ui_snapshot_delay_ms: int = 800
    ui_snapshot_min_interval_ms: int = 800
    logcat_json: bool = True


def load_profiles(path: str | Path) -> dict[str, AndroidAppProfile]:
    source = Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not read Android app profiles {source}: {exc}") from exc
    apps = raw.get("apps", {}) if isinstance(raw, dict) else {}
    if not isinstance(apps, dict) or not apps:
        raise ValueError(f"No Android app profiles found in {source}")
    profiles: dict[str, AndroidAppProfile] = {}
    for name, value in apps.items():
        if not isinstance(value, dict):
            raise ValueError(f"Android app profile {name!r} must be a mapping")
        app_id = str(value.get("app_id", "")).strip()
        flow_value = str(value.get("flows", "")).strip()
        flows = Path(flow_value) if flow_value else None
        proxy = value.get("proxy", {}) or {}
        capture = value.get("capture", {}) or {}
        network_source = str(value.get("network_source", "proxy")).casefold()
        configured_sources = value.get("network_sources")
        if configured_sources is None:
            if network_source in {"har", "json"}:
                configured_sources = ["app_log"]
            else:
                configured_sources = [network_source]
        if not isinstance(configured_sources, list) or not configured_sources:
            raise ValueError(f"network_sources for {name!r} must be a non-empty ordered list")
        network_sources = tuple(str(item).casefold() for item in configured_sources)
        allow_prefix_replay = value.get("allow_prefix_replay", False)
        hosts = proxy.get("allowed_hosts", []) or []
        if not app_id or flows is None:
            raise ValueError(f"Android app profile {name!r} requires app_id and flows")
        if not isinstance(hosts, list) or any(not isinstance(host, str) for host in hosts):
            raise ValueError(f"allowed_hosts for {name!r} must be a list of host patterns")
        if any(item not in {"proxy", "app_log", "logcat"} for item in network_sources):
            raise ValueError(f"network_sources for {name!r} may contain proxy, app_log, or logcat")
        if not isinstance(allow_prefix_replay, bool):
            raise ValueError(f"allow_prefix_replay for {name!r} must be true or false")
        port = int(proxy.get("port", 8080))
        settle_ms = int(capture.get("settle_ms", 750))
        ui_snapshots = value.get("ui_snapshots", capture.get("ui_snapshots", False))
        maestro_permitted = value.get("maestro_permitted", True)
        snapshot_delay = int(capture.get("ui_snapshot_delay_ms", 800))
        snapshot_interval = int(capture.get("ui_snapshot_min_interval_ms", 800))
        logcat_json = bool(value.get("logcat_json", True))
        if not isinstance(ui_snapshots, bool) or not isinstance(maestro_permitted, bool):
            raise ValueError(f"ui_snapshots and maestro_permitted for {name!r} must be booleans")
        if not (1 <= port <= 65535) or settle_ms < 0 or snapshot_delay < 0 or snapshot_interval < 0:
            raise ValueError(f"Invalid proxy port or settle_ms in profile {name!r}")
        if not flows.is_absolute():
            flows = (source.parent / flows).resolve()
        profiles[str(name)] = AndroidAppProfile(
            name=str(name), app_id=app_id, flows=flows,
            expected_text=value.get("expected_text"),
            network_source=network_source,
            allow_prefix_replay=allow_prefix_replay,
            allowed_hosts=tuple(host.lower() for host in hosts),
            settle_ms=settle_ms,
            proxy_host=str(proxy.get("listen_host", "0.0.0.0")),
            proxy_port=port,
            network_sources=network_sources,
            ui_snapshots=ui_snapshots,
            maestro_permitted=maestro_permitted,
            ui_snapshot_delay_ms=snapshot_delay,
            ui_snapshot_min_interval_ms=snapshot_interval,
            logcat_json=logcat_json,
        )
    return profiles


def host_allowed(host: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """An empty allowlist is discovery mode and accepts all hosts."""
    host = (host or "").lower().rstrip(".")
    if not patterns:
        return True
    for pattern in patterns:
        pattern = pattern.lower().rstrip(".")
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == pattern:
            return True
    return False


def sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    """Normalize a network event and remove sensitive values before persistence."""
    url = str(event.get("url") or event.get("request_url") or "")
    parsed = urlsplit(url)
    method = str(event.get("method") or "GET").upper()
    request_headers = event.get("request_headers") or event.get("headers") or {}
    response_headers = event.get("response_headers") or {}
    request_body = event.get("payload", event.get("request_body", ""))
    response_body = event.get("response_body", "")
    if isinstance(request_body, (dict, list)):
        request_body = json.dumps(request_body, ensure_ascii=False)
    if isinstance(response_body, (dict, list)):
        response_body = json.dumps(response_body, ensure_ascii=False)
    request_text = _redact_android_body(request_body)
    response_content_type = ""
    for key, value in response_headers.items():
        if str(key).casefold() == "content-type":
            response_content_type = str(value).split(";", 1)[0].strip().casefold()
            break
    response_body_meta = None
    if isinstance(response_body, (bytes, bytearray)):
        response_raw = bytes(response_body)
    else:
        response_raw = str(response_body or "").encode("utf-8", errors="replace")
    if response_content_type.startswith("image/") or response_content_type in {
        "application/pdf", "application/x-pdf"
    }:
        response_body_meta = {
            "content_type": response_content_type,
            "size_bytes": len(response_raw),
            "sha256": hashlib.sha256(response_raw).hexdigest(),
        }
        response_text = json.dumps({"binary_body": response_body_meta}, ensure_ascii=False, sort_keys=True)
    else:
        if isinstance(response_body, (bytes, bytearray)):
            response_body = bytes(response_body).decode("utf-8", errors="replace")
        response_text = _redact_android_body(response_body)
    if len(request_text) > 5_000:
        request_text = "[omitted: request body exceeds 5000 characters]"
    if len(response_text) > 100_000:
        response_text = "[omitted: response body exceeds 100000 characters]"
    action_id = event.get("action_id") or event.get("step_id")
    if action_id is not None:
        action_id = str(action_id)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", action_id):
            action_id = None
    correlation = event.get("correlation_status")
    if correlation not in {"matched", "ambiguous", "explicit"}:
        correlation = None
    return {
        "method": method,
        "url": _redact_android_url(url),
        "host": (parsed.hostname or "").lower(),
        "status": event.get("status"),
        "request_headers": _redact_android_headers(request_headers),
        "payload": request_text,
        "response_headers": _redact_android_headers(response_headers),
        "response_body": response_text,
        "response_body_meta": response_body_meta,
        "ts": parse_timestamp(event.get("ts", event.get("timestamp"))),
        "response_ts": parse_timestamp(event.get("response_ts")),
        "duration_ms": event.get("duration_ms"),
        "action_id": action_id,
        "correlation_status": correlation,
        "entry_point": redact_marker_text(event.get("entry_point") or "Android imported network event"),
    }


def parse_timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # HAR timestamps are ISO strings. Numeric epochs in this format are seconds.
        return float(value)
    text = str(value)
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def redact_marker_text(value: Any) -> str:
    """Remove recognizable credentials/PII while retaining normal UI labels."""
    text = str(value or "")
    text = _EMAIL_ADDRESS_RE.sub("[REDACTED:email]", text)
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b",
                  "[REDACTED:token]", text)
    text = re.sub(
        r"(?i)(authorization|cookie|set-cookie|token|password|secret|api[_-]?key|csrf)([\"'=:\s]+)([^\"'<>,\s]+)",
        r"\1\2[REDACTED]", text,
    )
    text = re.sub(
        r"(?i)(otp|pin|passcode|verification[_ -]?code)([\"'=:\s]+)([^\"'<>,\s]+)",
        r"\1\2[REDACTED]", text,
    )
    return text


def sanitize_hierarchy(text: str) -> str:
    """Redact sensitive Android node values but keep useful labels/tree structure."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        text = redact_marker_text(text)
        return re.sub(
            r"(?i)(authorization|cookie|set-cookie|token|password|secret|api[_-]?key|csrf)([\"'=:\s]+)([^\"'<>,\s]+)",
            r"\1\2[REDACTED]", text,
        )
    for node in root.iter():
        sensitive_node = any(
            _android_sensitive(value)
            for key, value in node.attrib.items()
            if key.casefold() in {"resource-id", "resourceid", "id", "hint", "content-desc", "contentdescription"}
        )
        password_node = any(key.casefold() == "password" and value.casefold() == "true"
                            for key, value in node.attrib.items())
        editable_node = any(key.casefold() in {"editable", "ispassword"} and value.casefold() == "true"
                            for key, value in node.attrib.items())
        for key, value in list(node.attrib.items()):
            normalized = key.casefold().replace("_", "-")
            if (_android_sensitive(key)
                    or ((sensitive_node or password_node or editable_node)
                        and normalized in {"text", "value"})
                    or (normalized in {"text", "content-desc", "content-description"}
                        and _SENSITIVE_VALUE.search(value))):
                node.attrib[key] = "[REDACTED]"
            else:
                node.attrib[key] = redact_marker_text(value)
        if node.text:
            node.text = "[REDACTED]" if _SENSITIVE_VALUE.search(node.text) else redact_marker_text(node.text)
    return ET.tostring(root, encoding="unicode")


def _header_dict(headers: Any) -> dict[str, str]:
    if isinstance(headers, dict):
        return {str(k): str(v) for k, v in headers.items()}
    result: dict[str, str] = {}
    if isinstance(headers, list):
        for entry in headers:
            if isinstance(entry, dict) and entry.get("name") is not None:
                result[str(entry["name"])] = str(entry.get("value", ""))
    return result


def parse_network_file(path: str | Path, file_format: str) -> list[dict[str, Any]]:
    """Read HAR 1.2 or normalized JSON events; returned entries are sanitized."""
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read network export {source}: {exc}") from exc
    events: list[dict[str, Any]] = []
    if file_format.casefold() == "har":
        entries = data.get("log", {}).get("entries", []) if isinstance(data, dict) else []
        for entry in entries:
            request = entry.get("request", {})
            response = entry.get("response", {})
            content = response.get("content", {})
            response_body = content.get("text", "")
            media_type = next((str(header.get("value", "")).split(";", 1)[0].strip().casefold()
                               for header in response.get("headers", [])
                               if str(header.get("name", "")).casefold() == "content-type"), "")
            binary_body = media_type.startswith("image/") or media_type in {"application/pdf", "application/x-pdf"}
            if content.get("encoding") == "base64":
                try:
                    decoded = base64.b64decode(response_body)
                    response_body = decoded if binary_body else decoded.decode("utf-8", errors="replace")
                except (ValueError, TypeError):
                    response_body = b"" if binary_body else "[omitted: invalid base64 body]"
            post_data = request.get("postData", {})
            ts = parse_timestamp(entry.get("startedDateTime"))
            duration_ms = entry.get("time")
            response_ts = ts + float(duration_ms) / 1000 if ts is not None and duration_ms is not None else None
            events.append(sanitize_event({
                "method": request.get("method"), "url": request.get("url"),
                "status": response.get("status"),
                "request_headers": _header_dict(request.get("headers")),
                "payload": post_data.get("text", ""),
                "response_headers": _header_dict(response.get("headers")),
                "response_body": response_body,
                "ts": ts, "response_ts": response_ts,
                "duration_ms": duration_ms,
            }))
    elif file_format.casefold() == "json":
        raw_events = data.get("events", data) if isinstance(data, dict) else data
        if not isinstance(raw_events, list):
            raise ValueError("Normalized JSON export must be a list or an object with an 'events' list")
        for event in raw_events:
            if (not isinstance(event, dict) or not (event.get("url") or event.get("request_url"))
                    or not event.get("method")):
                raise ValueError("Each JSON event must be an object with method and url")
            events.append(sanitize_event(event))
    else:
        raise ValueError("Network export format must be 'har' or 'json'")
    return events


def validate_marked_flows(path: str | Path) -> list[str]:
    """Ensure each authored capture action is preceded by one stable step marker."""
    root = Path(path)
    files = [root] if root.is_file() else sorted(
        item for item in root.rglob("*") if item.is_file() and item.suffix.lower() in {".yaml", ".yml"}
    )
    seen: set[str] = set()
    found: list[str] = []
    ui_actions = {
        "launchApp", "tapOn", "tapOnPoint", "doubleTapOn", "longPressOn", "swipe",
        "scroll", "scrollUntilVisible", "inputText", "eraseText", "back", "pressKey",
        "openLink", "assertVisible", "assertNotVisible", "assertTrue", "assertFalse",
    }
    unsupported_control = {"runFlow", "repeat", "while", "foreach"}
    for flow in files:
        try:
            docs = list(yaml.safe_load_all(flow.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"Could not parse capture flow {flow}: {exc}") from exc
        commands = docs[1] if len(docs) > 1 else None
        if not isinstance(commands, list):
            raise ValueError(f"Capture flow {flow} must have a Maestro command list")
        if any(isinstance(command, dict) and unsupported_control.intersection(command) for command in commands):
            raise ValueError(f"Capture flow {flow} must be linear and top-level for hierarchy fallback")
        marker_indices: set[int] = set()
        end_indices: list[int] = []
        for index, command in enumerate(commands):
            if not isinstance(command, dict) or "runScript" not in command:
                continue
            spec = command["runScript"]
            if not isinstance(spec, dict) or not str(spec.get("file", "")).endswith("marker.js"):
                continue
            env = spec.get("env", {}) or {}
            step_id = str(env.get("STEP_ID", ""))
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", step_id):
                raise ValueError(f"Capture marker in {flow} must have a unique safe STEP_ID")
            if step_id in seen:
                raise ValueError(f"Duplicate capture STEP_ID {step_id!r}; use unique IDs across selected flows")
            seen.add(step_id)
            found.append(step_id)
            marker_indices.add(index)
            if step_id == "__end__":
                end_indices.append(index)
                if (index == 0 or not isinstance(commands[index - 1], dict)
                        or "waitForAnimationToEnd" not in commands[index - 1]):
                    raise ValueError(f"End marker in {flow} must be preceded by waitForAnimationToEnd")
                continue
            if index == 0:
                raise ValueError(f"Capture marker {step_id!r} in {flow} needs a settle wait before it")
            previous = commands[index - 1]
            if not isinstance(previous, dict) or "waitForAnimationToEnd" not in previous:
                raise ValueError(f"Capture marker {step_id!r} in {flow} must be preceded by waitForAnimationToEnd")
            next_index = index + 1
            while (next_index < len(commands) and isinstance(commands[next_index], dict)
                   and "waitForAnimationToEnd" in commands[next_index]):
                next_index += 1
            if next_index >= len(commands):
                raise ValueError(f"Capture marker {step_id!r} in {flow} has no following action")
        for index, command in enumerate(commands):
            if not isinstance(command, dict) or not ui_actions.intersection(command):
                continue
            marker_index = index - 1
            if marker_index not in marker_indices:
                raise ValueError(f"UI action in {flow} at command {index + 1} needs an immediately preceding marker.js step")
            marker = commands[marker_index]["runScript"]
            marker_env = marker.get("env", {}) if isinstance(marker, dict) else {}
            if marker_env.get("STEP_ID") == "__end__":
                raise ValueError(f"End marker in {flow} cannot precede a UI action")
        if len(end_indices) != 1 or end_indices[0] != len(commands) - 1:
            raise ValueError(f"Capture flow {flow} must end with exactly one __end__ marker")
    if not found:
        raise ValueError(f"No runScript marker.js steps found in {root}; mark each action before capture")
    return found


def assign_events_to_markers(markers: list[dict], events: list[dict],
                             max_window_seconds: float = 30.0) -> list[dict]:
    """Map a complete request to one marker window; spanning/late calls are ambiguous."""
    ordered = sorted(markers, key=lambda marker: float(marker["ts"]))
    output = []
    for event in events:
        if event.get("action_id"):
            output.append({**event, "correlation_status": event.get("correlation_status") or "explicit",
                           "correlation_reason": "explicit step reference in imported event"})
            continue
        start = event.get("ts")
        finish = event.get("response_ts") or start
        action_id = None
        status = "ambiguous"
        reason = "outside marker windows"
        if start is not None:
            for i, marker in enumerate(ordered):
                left = float(marker["ts"])
                right = float(ordered[i + 1]["ts"]) if i + 1 < len(ordered) else None
                if start >= left and (right is None or start < right):
                    if right is not None and finish is not None and finish >= right:
                        reason = "response arrived after the next marker"
                    elif finish is not None and finish - left > max_window_seconds:
                        reason = "request arrived late after the interaction window"
                    else:
                        action_id = marker.get("step_id")
                        status = "matched"
                        reason = "request completed inside marker window"
                    break
        output.append({**event, "action_id": action_id,
                       "correlation_status": status, "correlation_reason": reason})
    return output


class CaptureCollector:
    """Local endpoint for Maestro markers and sanitized mitmproxy events."""

    def __init__(self, allowed_hosts: tuple[str, ...] = (), on_marker=None):
        self.allowed_hosts = allowed_hosts
        self.on_marker = on_marker
        self.markers: list[dict] = []
        self.events: list[dict] = []
        self.hosts: set[str] = set()
        self.host_failures: dict[str, dict[str, int]] = {}
        self.proxy_diagnostics = {
            name: 0 for name in (
                "client_connections", "client_connections_from_device", "other_client_connections",
                "client_disconnects", "proxy_connect_requests",
                "http_requests", "http_responses", "http_errors",
                "client_tls_successes", "client_tls_failures",
                "server_tls_successes", "server_tls_failures",
                "upstream_connections", "upstream_connection_failures",
                "tcp_flows", "udp_flows",
            )
        }
        self.proxy_host_diagnostics: dict[str, dict[str, int]] = {}
        self.errors: list[str] = []
        self.accept_markers = True
        self.accept_events = True
        self.lock = threading.Lock()
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size > 8_000_000:
                        self.send_error(413)
                        return
                    payload = json.loads(self.rfile.read(size) or b"{}")
                    if self.path == "/marker":
                        if not collector.accept_markers:
                            body = b'{"ok":true,"disabled":true}'
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                            return
                        item = {
                            "step_id": str(payload.get("step_id", "")),
                            "label": redact_marker_text(payload.get("label", "")),
                            "action_kind": str(payload.get("action_kind", "tap")),
                            "action_target": redact_marker_text(payload.get("action_target", "")),
                            "ts": time.time(),
                        }
                        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", item["step_id"]):
                            self.send_error(400, "step_id must be 1-80 letters, numbers, dot, colon, underscore, or dash")
                            return
                        with collector.lock:
                            collector.markers.append(item)
                        if collector.on_marker is not None:
                            try:
                                item.update(collector.on_marker(item) or {})
                            except Exception as exc:  # keep the flow moving; report snapshot failure
                                item["hierarchy_error"] = str(exc)
                        body = json.dumps({"ok": True, "ts": item["ts"]}).encode()
                    elif self.path == "/event":
                        item = sanitize_event(payload)
                        if not collector.accept_events or not host_allowed(item["host"], collector.allowed_hosts):
                            body = json.dumps({"ok": True, "filtered": True}).encode()
                        else:
                            item["received_ts"] = time.time()
                            with collector.lock:
                                if item["host"]:
                                    collector.hosts.add(item["host"])
                                collector.events.append(item)
                            body = json.dumps({"ok": True}).encode()
                    elif self.path == "/failure":
                        host = str(payload.get("host", "")).lower().rstrip(".")
                        kind = str(payload.get("kind", "connection"))
                        if kind not in {"connection", "tls"}:
                            self.send_error(400, "unsupported failure kind")
                            return
                        if host and host_allowed(host, collector.allowed_hosts):
                            with collector.lock:
                                counts = collector.host_failures.setdefault(
                                    host, {"connection_failures": 0, "tls_failures": 0}
                                )
                                counts["tls_failures" if kind == "tls" else "connection_failures"] += 1
                        body = json.dumps({"ok": True}).encode()
                    elif self.path == "/diagnostic":
                        kind = str(payload.get("kind", ""))
                        host = str(payload.get("host", "")).casefold().rstrip(".")
                        if kind not in collector.proxy_diagnostics:
                            self.send_error(400, "unsupported proxy diagnostic")
                            return
                        if host and not host_allowed(host, collector.allowed_hosts):
                            body = json.dumps({"ok": True, "filtered": True}).encode()
                        else:
                            with collector.lock:
                                collector.proxy_diagnostics[kind] += 1
                                if host:
                                    counts = collector.proxy_host_diagnostics.setdefault(host, {})
                                    counts[kind] = counts.get(kind, 0) + 1
                            body = json.dumps({"ok": True}).encode()
                    else:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (ValueError, json.JSONDecodeError) as exc:
                    self.send_error(400, str(exc))

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.started = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self):
        self.thread.start()
        self.started = True
        return self

    def close(self):
        if not self.started:
            self.server.server_close()
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)



"""URL normalization shared by testcase generation and replay."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def normalize_replay_url(value: object) -> str:
    """Add a scheme to a host-only replay URL while preserving explicit URLs."""
    text = str(value or "").strip()
    if not text or "://" in text:
        return text
    if text.startswith("//"):
        return "https:" + text
    if text.startswith("/"):
        return text

    parsed = urlsplit("//" + text)
    host = parsed.hostname or ""
    if not host or any(character.isspace() for character in host):
        return text
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is None and host.casefold() != "localhost" and "." not in host:
        return text
    local = host.casefold() in {"localhost"} or host.casefold().endswith(".local")
    if address is not None and (address.is_private or address.is_loopback or address.is_link_local):
        local = True
    scheme = "http" if local else "https"
    return f"{scheme}://{text}"

"""Small JSON-chat adapter for Anthropic's Messages API."""

from __future__ import annotations

import json
import os
import re

import httpx

from capture_app.integrations.openrouter import OpenRouterError


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")


def messages_json(
    system_prompt: str,
    user_prompt: str,
    model: str | None = None,
    temperature: float = 0.4,
    max_tokens: int | None = None,
    max_retries: int = 0,
):
    """Call Claude and parse its text response as JSON.

    ``temperature`` is accepted for provider interface compatibility. The
    request omits it so models that don't support temperature overrides work.
    """
    del temperature
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise OpenRouterError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Create an API key in the Anthropic Console and add it to .env."
        )

    selected_model = model or DEFAULT_MODEL
    payload = {
        "model": selected_model,
        "max_tokens": int(max_tokens or 3000),
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    attempt = 0
    while True:
        attempt += 1
        try:
            response = httpx.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=60)
        except httpx.RequestError as exc:
            if attempt <= max_retries:
                continue
            raise OpenRouterError(f"Network error calling Anthropic: {exc}") from exc

        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code < 200 or response.status_code >= 300:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            message = str(error.get("message") or response.text[:500])
            retryable = response.status_code in {408, 429, 500, 502, 503, 504}
            if retryable and attempt <= max_retries:
                continue
            raise OpenRouterError(
                f"Anthropic returned {response.status_code}: {message}",
                failure_kind="rate_limited" if response.status_code == 429 else None,
            )

        try:
            blocks = data["content"]
            stop_reason = data.get("stop_reason")
            text = "\n".join(
                block["text"] for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ).strip()
        except (KeyError, TypeError, AttributeError):
            raise OpenRouterError(
                "Anthropic returned an incomplete Messages API response.",
                failure_kind="invalid_response",
            )
        if not text:
            raise OpenRouterError(
                "Anthropic returned no usable text content.",
                failure_kind="truncated" if stop_reason == "max_tokens" else "invalid_response",
            )

        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            truncated = stop_reason == "max_tokens"
            raise OpenRouterError(
                f"Model returned invalid JSON{' before truncation' if truncated else ''}: {exc}\n"
                f"Raw output: {text[:500]}",
                raw_output=text,
                failure_kind="truncated" if truncated else "invalid_json",
            ) from exc

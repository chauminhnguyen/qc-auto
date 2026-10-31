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


class JSONResponse(dict):
    """Parsed JSON object with non-serialized Anthropic response metadata."""

    def __init__(self, value: dict, response_metadata: dict):
        super().__init__(value)
        self.response_metadata = response_metadata


def messages_json(
    system_prompt: str,
    user_prompt: str,
    model: str | None = None,
    temperature: float | None = 0.4,
    max_tokens: int | None = None,
    max_retries: int = 0,
    output_schema: dict | None = None,
):
    """Call Claude and parse its text response as JSON.

    Temperature is forwarded when supplied; omit it for Claude models that do not accept it.
    """
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
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if output_schema is not None:
        payload["output_config"] = {
            "format": {"type": "json_schema", "schema": output_schema},
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
                raw_response=response.text,
            )

        if not isinstance(data, dict):
            raise OpenRouterError(
                "Anthropic returned an invalid Messages API response: "
                "stop_reason=unknown, output_tokens=unknown.",
                failure_kind="invalid_response",
                raw_response=response.text,
                stop_reason="unknown",
            )
        stop_reason = data.get("stop_reason")
        usage = data.get("usage") or {}
        output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
        blocks = data.get("content")
        text = "\n".join(
            block["text"] for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ).strip() if isinstance(blocks, list) else ""
        if stop_reason != "end_turn":
            reason = str(stop_reason or "unknown")
            token_count = output_tokens if output_tokens is not None else "unknown"
            if reason == "refusal":
                failure_kind = "refusal"
            elif reason == "max_tokens":
                failure_kind = "truncated"
            else:
                failure_kind = "invalid_response"
            raise OpenRouterError(
                f"Anthropic response was not complete: stop_reason={reason}, "
                f"output_tokens={token_count}.",
                raw_output=text or None,
                failure_kind=failure_kind,
                raw_response=response.text,
                stop_reason=reason,
                output_tokens=output_tokens,
            )
        if not isinstance(blocks, list):
            raise OpenRouterError(
                f"Anthropic returned an incomplete Messages API response: "
                f"stop_reason=end_turn, output_tokens={output_tokens if output_tokens is not None else 'unknown'}.",
                failure_kind="invalid_response",
                raw_response=response.text,
                stop_reason="end_turn",
                output_tokens=output_tokens,
            )
        if not text:
            raise OpenRouterError(
                f"Anthropic returned no usable text content: stop_reason=end_turn, "
                f"output_tokens={output_tokens if output_tokens is not None else 'unknown'}.",
                failure_kind="invalid_response",
                raw_response=response.text,
                stop_reason="end_turn",
                output_tokens=output_tokens,
            )

        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OpenRouterError(
                f"Anthropic returned invalid JSON: {exc}; stop_reason=end_turn, "
                f"output_tokens={output_tokens if output_tokens is not None else 'unknown'}.",
                raw_output=text,
                failure_kind="invalid_json",
                raw_response=response.text,
                stop_reason="end_turn",
                output_tokens=output_tokens,
            ) from exc
        if not isinstance(parsed, dict):
            raise OpenRouterError(
                f"Anthropic JSON output was not an object: stop_reason=end_turn, "
                f"output_tokens={output_tokens if output_tokens is not None else 'unknown'}.",
                raw_output=text,
                failure_kind="invalid_response",
                raw_response=response.text,
                stop_reason="end_turn",
                output_tokens=output_tokens,
            )
        return JSONResponse(parsed, {
            "stop_reason": stop_reason,
            "output_tokens": output_tokens,
        })

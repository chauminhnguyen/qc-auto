"""
openrouter.py
-------------
Thin wrapper around OpenRouter's OpenAI-compatible chat completions endpoint.
Used by enrich.py to (1) generate payload variants and (2) synthesize param
definitions from the resulting responses.

Requires the OPENROUTER_API_KEY environment variable (free key:
https://openrouter.ai/keys).

Model is configurable via OPENROUTER_MODEL because free-tier model IDs on
OpenRouter rotate over time. If the default below has been retired, check
https://openrouter.ai/models?max_price=0 for a current ":free" model and
either export OPENROUTER_MODEL or edit DEFAULT_MODEL.

The shared output budget is configurable with OPENROUTER_MAX_TOKENS. Reasoning
effort defaults to "none" for JSON tasks so hidden reasoning does not consume
the completion budget needed for the actual response.
"""

from __future__ import annotations

import json
import os
import random
import time
from capture_app.env import load_project_env

load_project_env()
import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
DEFAULT_MAX_TOKENS = int(os.environ.get("OPENROUTER_MAX_TOKENS", "3000"))
REASONING_EFFORT = os.environ.get("OPENROUTER_REASONING_EFFORT", "none").strip().lower()

# Transient upstream failures worth retrying -- these are the target model's
# host (e.g. Nvidia) being overloaded/unreachable, not a problem with our
# request, so a short backoff and another try is usually all it takes.
RETRYABLE_STATUS_CODES = {408, 429, 502, 503, 504}
RETRYABLE_ERROR_CODES = {408, 429, 502, 503, 504}

DEFAULT_MAX_RETRIES = int(os.environ.get("OPENROUTER_MAX_RETRIES", "3"))
DEFAULT_RETRY_BACKOFF_SECONDS = float(os.environ.get("OPENROUTER_RETRY_BACKOFF", "2.0"))


class OpenRouterError(RuntimeError):
    def __init__(self, message: str, raw_output: str | None = None,
                 failure_kind: str | None = None):
        super().__init__(message)
        self.raw_output = raw_output
        self.failure_kind = failure_kind


def _extract_error_code(data) -> int | None:
    """OpenRouter sometimes returns HTTP 200 with an {'error': {...}} body
    (e.g. when the upstream free-tier provider is overloaded), instead of a
    non-200 status. Pull the code out of either shape."""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            try:
                return int(code) if code is not None else None
            except (TypeError, ValueError):
                return None
    return None


def _sleep_backoff(attempt: int, base: float) -> None:
    # Exponential backoff with a little jitter so a batch of retries doesn't
    # all land on OpenRouter at the exact same moment.
    delay = base * (2 ** (attempt - 1)) + random.uniform(0, base)
    time.sleep(delay)


def chat_json(system_prompt: str, user_prompt: str, model: str | None = None,
              temperature: float = 0.4, max_tokens: int | None = None,
              max_retries: int = DEFAULT_MAX_RETRIES,
              retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS):
    """
    Calls OpenRouter, instructs the model to answer with JSON only, and
    parses that JSON. Raises OpenRouterError with a readable message on any
    failure (missing key, network error, bad status, unparsable output).

    Transient upstream errors (408/429/502/503/504 -- e.g. "Service
    temporarily overloaded") are retried automatically up to `max_retries`
    times with exponential backoff before giving up. This retry is scoped
    entirely to this single OpenRouter call -- it never re-runs whatever
    caller-side work (like live HTTP requests to a target API) produced the
    prompt in the first place.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise OpenRouterError(
            "OPENROUTER_API_KEY environment variable is not set. "
            "Get a free key at https://openrouter.ai/keys"
        )

    selected_model = model or DEFAULT_MODEL
    selected_max_tokens = DEFAULT_MAX_TOKENS if max_tokens is None else int(max_tokens)
    if "embed" in selected_model.lower():
        raise OpenRouterError(
            f"{selected_model!r} appears to be an embedding model and cannot be used "
            "with the chat/completions endpoint. Set OPENROUTER_MODEL to a chat model."
        )

    payload = {
        "model": selected_model,
        "temperature": temperature,
        "max_tokens": selected_max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    # Reasoning tokens can consume the entire completion budget without
    # leaving any JSON for the caller. Empty setting disables this override.
    if REASONING_EFFORT:
        payload["reasoning"] = {"effort": REASONING_EFFORT}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    attempt = 0
    while True:
        attempt += 1
        try:
            resp = httpx.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)
        except httpx.RequestError as e:
            if attempt <= max_retries:
                print(f"  [openrouter] network error (attempt {attempt}/{max_retries + 1}): {e} -- retrying...")
                _sleep_backoff(attempt, retry_backoff)
                continue
            raise OpenRouterError(f"Network error calling OpenRouter: {e}") from e

        # Try to parse the body even on non-200 -- OpenRouter sometimes puts
        # the real error info in JSON regardless of status code.
        try:
            data = resp.json()
        except ValueError:
            data = None

        error_code = _extract_error_code(data)
        is_error = resp.status_code != 200 or error_code is not None
        if is_error:
            reason = data.get("error", {}).get("message") if isinstance(data, dict) else resp.text[:500]
            reason = str(reason or "")
            reason_lower = reason.casefold()
            quota_exhausted = any(marker in reason_lower for marker in (
                "free-models-per-day", "daily quota", "quota exceeded", "daily limit",
                "credits to unlock", "insufficient credits",
            ))
            if quota_exhausted:
                raise OpenRouterError(
                    f"OpenRouter quota exhausted: {reason}",
                    failure_kind="quota_exhausted",
                )
            retryable = resp.status_code in RETRYABLE_STATUS_CODES or error_code in RETRYABLE_ERROR_CODES
            if retryable and attempt <= max_retries:
                print(f"  [openrouter] transient error (attempt {attempt}/{max_retries + 1}): {reason} -- retrying...")
                _sleep_backoff(attempt, retry_backoff)
                continue
            body_preview = data if data is not None else resp.text[:500]
            failure_kind = "rate_limited" if resp.status_code == 429 or error_code == 429 else None
            raise OpenRouterError(
                f"OpenRouter returned {resp.status_code}: {body_preview}",
                failure_kind=failure_kind,
            )

        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content")
            finish_reason = choice.get("finish_reason")
            usage = data.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
        except (KeyError, IndexError, AttributeError, TypeError):
            raise OpenRouterError("OpenRouter returned an incomplete chat response.",
                                  failure_kind="invalid_response")

        # Do not include the provider's `reasoning` field in exceptions or
        # logs. It can be very large and is not the user-facing answer.
        if not isinstance(content, str) or not content.strip():
            completion_tokens = usage.get("completion_tokens", "unknown")
            token_details = usage.get("completion_tokens_details") or {}
            if not isinstance(token_details, dict):
                token_details = {}
            reasoning_tokens = token_details.get("reasoning_tokens", "unknown")
            if finish_reason == "length":
                raise OpenRouterError(
                    "Model output was truncated at the token limit "
                    f"(finish_reason=length, completion_tokens={completion_tokens}, "
                    f"reasoning_tokens={reasoning_tokens}).",
                    failure_kind="truncated",
                )
            raise OpenRouterError(
                "OpenRouter returned no usable text content "
                f"(finish_reason={finish_reason or 'unknown'}).",
                failure_kind="invalid_response",
            )

        text = content.strip()

        # Some free models wrap JSON in a markdown fence despite instructions -- strip it.
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            truncated = finish_reason == "length"
            truncation_note = " before truncation" if truncated else ""
            raise OpenRouterError(
                f"Model returned invalid JSON{truncation_note}: {e}\n"
                f"Raw output: {text[:500]}",
                raw_output=text,
                failure_kind="truncated" if truncated else "invalid_json",
            )

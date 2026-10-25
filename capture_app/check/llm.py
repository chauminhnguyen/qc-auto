from __future__ import annotations

import json
import os
import re
import threading
import time
from email.utils import parsedate_to_datetime

from capture_app.env import load_project_env


class CallLimit(Exception):
    pass


class LLMClient:
    def __init__(self, provider: str, *, max_calls: int = 0, dry_run: bool = False):
        load_project_env()
        self.provider = provider
        self.max_calls = max_calls
        self.dry_run = dry_run
        self.calls = 0
        self.cache_hits = 0
        self._lock = threading.Lock()

    def complete_json(self, system: str, user: str, model: str, max_tokens: int = 3000) -> dict:
        if self.dry_run:
            raise RuntimeError("LLM calls are disabled in dry-run mode")
        if self.provider == "openrouter":
            from capture_app.integrations.openrouter import chat_json
            try:
                result = chat_json(system, user, model=model, temperature=0, max_tokens=max_tokens, call_callback=self._start_call)
            except Exception as error:
                if getattr(error, "failure_kind", None) in {"invalid_json", "truncated"}:
                    return {"_text": getattr(error, "raw_output", None) or ""}
                raise
            return result if isinstance(result, dict) else {"_value": result}
        return self._claude(system, user, model, max_tokens, self._start_call)

    def _start_call(self):
        with self._lock:
            if self.max_calls and self.calls >= self.max_calls:
                raise CallLimit
            self.calls += 1

    @staticmethod
    def _claude(system: str, user: str, model: str, max_tokens: int, call_callback):
        import httpx
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError("Claude provider requires ANTHROPIC_API_KEY")
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        payload = {"model": model, "max_tokens": max_tokens, "temperature": 0,
                   "system": system, "messages": [{"role": "user", "content": user}]}
        for attempt in range(4):
            try:
                call_callback()
                response = httpx.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=60)
            except httpx.TimeoutException:
                if attempt == 3: raise
                time.sleep(min(0.5 * 2**attempt, 8)); continue
            if response.status_code == 400 and "temperature" in payload:
                payload.pop("temperature")
                call_callback()
                response = httpx.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=60)
            if response.status_code not in {429, 500, 502, 503, 529} or attempt == 3:
                response.raise_for_status(); break
            retry_after = response.headers.get("retry-after")
            try: delay = float(retry_after) if retry_after else min(0.5 * 2**attempt, 8)
            except ValueError:
                try: delay = max(0.0, parsedate_to_datetime(retry_after).timestamp()-time.time())
                except (TypeError, ValueError, OverflowError): delay = min(0.5 * 2**attempt, 8)
            time.sleep(delay)
        body = response.json()
        return {"_text": "\n".join(x.get("text", "") for x in body.get("content", []) if x.get("type") == "text")}


def parse_json(value):
    if isinstance(value, (dict, list)):
        return value
    text = str(value).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def provider_from_env(provider=None):
    load_project_env()
    selected = (provider or os.environ.get("CHECK_PROVIDER", "")).lower()
    if selected and selected not in {"claude", "openrouter"}:
        raise ValueError("CHECK_PROVIDER must be claude or openrouter")
    if not selected:
        selected = "claude" if os.environ.get("ANTHROPIC_API_KEY") else "openrouter" if os.environ.get("OPENROUTER_API_KEY") else ""
    if not selected:
        raise ValueError("Set ANTHROPIC_API_KEY or OPENROUTER_API_KEY (or CHECK_PROVIDER with the matching key)")
    key = "ANTHROPIC_API_KEY" if selected == "claude" else "OPENROUTER_API_KEY"
    if not os.environ.get(key):
        raise ValueError(f"{selected} provider requires {key}")
    return selected


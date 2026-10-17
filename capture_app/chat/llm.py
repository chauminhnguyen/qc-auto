from __future__ import annotations

import json
import os
from urllib import request, error

from capture_app.env import load_project_env


class ChatCompletionClient:
    def __init__(self, provider: str, api_base: str, model: str, api_key: str = ""):
        self.provider = provider
        self.api_base = api_base
        self.model = model
        self.api_key = api_key

    def complete_with_usage(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> tuple[str, dict]:
        payload = {
            "model": self.model,
            "messages": [],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system_prompt:
            payload["messages"].append({"role": "system", "content": system_prompt})
        payload["messages"].append({"role": "user", "content": prompt})
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(self.api_base, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:  # pragma: no cover - network path
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"LLM call failed: {exc.code} {body}") from exc
        try:
            resp_json = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON response from provider: {raw[:200]}") from exc
        if "choices" not in resp_json or not resp_json["choices"]:
            raise RuntimeError(f"No completion choices returned: {raw[:200]}")
        choice = resp_json["choices"][0]
        message = choice.get("message") or {}
        if isinstance(message, dict):
            answer = str(message.get("content") or "")
        else:
            answer = str(choice.get("text") or "")
        return answer, resp_json

    def complete(self, prompt: str, system_prompt: str | None = None, temperature: float = 0.1, max_tokens: int = 1024) -> str:
        answer, _ = self.complete_with_usage(prompt, system_prompt=system_prompt, temperature=temperature, max_tokens=max_tokens)
        return answer


def provider_from_env(provider: str | None = None, api_base: str | None = None, model: str | None = None, api_key: str | None = None):
    load_project_env()
    provider = (provider or os.environ.get("CHAT_PROVIDER") or "local").lower()
    model = model or os.environ.get("CHAT_MODEL") or "llama3.1"
    api_base = api_base or os.environ.get("CHAT_API_BASE") or (
        "http://127.0.0.1:11434/v1/chat/completions" if provider == "local" else "https://openrouter.ai/api/v1/chat/completions"
    )
    key = api_key or os.environ.get("CHAT_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
    return ChatCompletionClient(provider=provider, api_base=api_base, model=model, api_key=key)

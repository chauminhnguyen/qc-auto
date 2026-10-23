from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

from capture_app.env import load_project_env


@dataclass
class ChatConfig:
    vault_path: Path = Path("./ObsidianVault")
    db_path: Path | None = None
    provider: str = "local"
    model: str = "llama3.1"
    api_base: str = "http://127.0.0.1:11434/v1/chat/completions"
    api_key: str = ""
    temperature: float = 0.1
    max_tokens: int = 1024
    num_ctx: int = 4096
    include_dirs: tuple[str, ...] = ("APIs", "Params", "Flows", "Knowledge")
    exclude_dirs: tuple[str, ...] = (".capture_index", ".obsidian", ".chat_index", "Screenshots")

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> ChatConfig:
        arg_mapping = {
            "db": "db_path",
            "provider": "provider",
            "model": "model",
            "api_base": "api_base",
            "api_key": "api_key",
            "temperature": "temperature",
            "max_tokens": "max_tokens",
        }
        overrides = {
            config_name: value
            for arg_name, config_name in arg_mapping.items()
            if (value := getattr(args, arg_name, None)) is not None
        }
        return cls.from_vault(getattr(args, "vault", None), **overrides)

    @classmethod
    def from_vault(cls, vault_path: str | Path | None = None, **overrides):
        # Chat model/provider settings should have one source of truth: the
        # project .env, even if this PowerShell session has stale variables.
        load_project_env(override=True)
        vault = Path(vault_path or os.environ.get("CAPTURE_VAULT_PATH", "./ObsidianVault")).resolve()
        provider = overrides.pop("provider", None) or os.environ.get("CHAT_PROVIDER", "local")
        provider = provider.lower()
        model = overrides.pop("model", None) or os.environ.get("CHAT_MODEL")
        if not model:
            if provider == "anthropic":
                model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
            elif provider == "openrouter":
                model = os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
            else:
                model = "llama3.1"

        default_api_base = {
            "local": "http://127.0.0.1:11434/v1/chat/completions",
            "openrouter": "https://openrouter.ai/api/v1/chat/completions",
            "anthropic": "https://api.anthropic.com/v1/messages",
        }.get(provider, "http://127.0.0.1:11434/v1/chat/completions")
        api_base = overrides.pop("api_base", None) or os.environ.get("CHAT_API_BASE", default_api_base)
        api_key = overrides.pop("api_key", None) or os.environ.get("CHAT_API_KEY")
        if not api_key:
            key_env = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENROUTER_API_KEY"
            api_key = os.environ.get(key_env, "")
        temperature_value = overrides.pop("temperature", None)
        max_tokens_value = overrides.pop("max_tokens", None)
        temperature = float(
            temperature_value if temperature_value is not None
            else os.environ.get("CHAT_TEMPERATURE", "0.1")
        )
        max_tokens = int(
            max_tokens_value if max_tokens_value is not None
            else os.environ.get("CHAT_MAX_TOKENS", "1024")
        )
        num_ctx = int(overrides.pop("num_ctx", os.environ.get("CHAT_NUM_CTX", "4096")))
        return cls(
            vault_path=vault,
            db_path=Path(overrides.pop("db_path", vault / ".chat_index" / "index.sqlite")),
            provider=provider,
            model=model,
            api_base=api_base,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            num_ctx=num_ctx,
            **overrides,
        )


def default_chat_config(vault_path: str | Path | None = None, **overrides) -> ChatConfig:
    return ChatConfig.from_vault(vault_path=vault_path, **overrides)

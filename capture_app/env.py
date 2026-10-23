"""Shared loading of project-level environment configuration."""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_project_env(override: bool = False) -> None:
    """Load the repository's .env, optionally making it authoritative."""
    load_dotenv(PROJECT_ROOT / ".env", override=override)

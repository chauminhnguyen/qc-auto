"""Shared loading of project-level environment configuration."""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_project_env() -> None:
    """Load the repository's .env without overriding shell-provided values."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)

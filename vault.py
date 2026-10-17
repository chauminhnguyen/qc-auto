"""Compatibility entrypoint for the vault API and interactive cleanup."""

from pathlib import Path

from capture_app.storage.vault import *
from capture_app.storage.vault import VaultWriter


if __name__ == "__main__":
    vault_path = Path(input("Obsidian vault folder path [./ObsidianVault]: ").strip() or "./ObsidianVault")
    VaultWriter(vault_path).review_interactive()
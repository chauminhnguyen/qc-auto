"""Chat/RAG layer for querying the vault with a dedicated SQLite search index."""

from .config import ChatConfig
from .db import ChatDB
from .ingest import build_index
from .retriever import SearchEngine

__all__ = ["ChatConfig", "ChatDB", "build_index", "SearchEngine"]

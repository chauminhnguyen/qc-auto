"""BRD ingestion and triage utilities.

This package provides the first phase of the BRD pipeline: source profiling,
file hashing, tier assignment, and inventory persistence. The rest of the
BRD extractor pipeline can grow here without touching the existing vault or
capture modules.
"""

from .triage import FileProfile, InventoryDB, classify_brd_file, scan_brd_directory
from .ledger import BrdLedger
from .ir import DocumentProfile, ExtractedImage, ExtractedTable, FigureBlock, PageChunk, SourceAnchor

__all__ = [
    "FileProfile",
    "InventoryDB",
    "classify_brd_file",
    "scan_brd_directory",
    "BrdLedger",
    "DocumentProfile",
    "PageChunk",
    "ExtractedImage",
    "ExtractedTable",
    "FigureBlock",
    "SourceAnchor",
]

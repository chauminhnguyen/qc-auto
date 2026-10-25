"""Deterministic graph retrieval weights and limits for chat."""

# Note relationship weights.
EDGE_NEXT_PREV = 0.9
EDGE_PART_OF = 0.8
EDGE_CROSS_REFERENCE = 0.8
EDGE_WIKILINK = 0.3
EDGE_SHARED_ENTITY_MIN = 0.3
EDGE_SHARED_ENTITY_MAX = 0.7
EDGE_SAME_NOTE_NEIGHBOR = 0.9

# Retrieval limits and scoring.
SHARED_ENTITY_MAX_NOTES = 20
MAX_HOPS = 1
HOP_DECAY = 0.7
MIN_EXPANSION_SCORE = 0.05
EXPAND_BUDGET_CHARS = 8000
BM25_SEED_SCORE_FLOOR = 0.05
MAX_EXPANDED_CHUNKS_PER_NOTE = 2

# Terms are taken only from explicit metadata and recognizable identifiers.
MIN_ENTITY_LENGTH = 2

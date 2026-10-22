"""Configuration for the non-generative Jira retrieval path."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JiraIngestConfig:
    depth: int = 2
    max_issues: int = 100
    include_bugs: bool = False
    link_types: tuple[str, ...] = ()
    filter_noise: bool = True
    chunk_chars: int = 8000
    top_k: int = 10
    query_terms: int = 12
    stopwords: tuple[str, ...] = (
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "has", "have", "in", "is", "it", "of", "on", "or", "the", "this",
        "that", "to", "was", "were", "with",
    )
    relative_threshold: float = 0.55
    min_matched_terms: int = 2
    rrf_k: int = 60
    anchor_weight: float = 1.5
    bm25_weight: float = 1.0
    dense_weight: float = 1.0
    graph_weight: float = 0.15
    graph_seeds: int = 5
    dense_enabled: bool = False
    dense_model: str = ""

    @classmethod
    def load(cls, path: Path = Path("config.yaml")) -> "JiraIngestConfig":
        path = Path(path)
        if not path.is_file():
            return cls()
        try:
            import yaml
        except ImportError as error:
            raise ValueError(
                "Reading config.yaml requires PyYAML; install project dependencies with "
                "python -m pip install -r requirements.txt"
            ) from error
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise ValueError(f"Could not read Jira configuration from {path}: {error}") from error
        if not isinstance(document, dict):
            raise ValueError(f"Jira configuration in {path} must be a YAML mapping")
        raw = document.get("knowledge_jira", {}) or {}
        if not isinstance(raw, dict):
            raise ValueError("config.yaml knowledge_jira must be a YAML mapping")
        scope = raw.get("scope", {}) or {}
        comments = raw.get("comments", {}) or {}
        retrieval = raw.get("retrieval", {}) or {}
        graph = raw.get("graph", {}) or {}
        dense = raw.get("dense", {}) or {}
        for name, section in (("scope", scope), ("comments", comments),
                              ("retrieval", retrieval), ("graph", graph), ("dense", dense)):
            if not isinstance(section, dict):
                raise ValueError(f"config.yaml knowledge_jira.{name} must be a YAML mapping")
        link_types = scope.get("link_types", [])
        if not isinstance(link_types, list) or any(not isinstance(item, str) for item in link_types):
            raise ValueError("config.yaml knowledge_jira.scope.link_types must be a list of strings")
        stopwords = retrieval.get("stopwords", list(cls.stopwords))
        if not isinstance(stopwords, list) or any(not isinstance(item, str) for item in stopwords):
            raise ValueError("config.yaml knowledge_jira.retrieval.stopwords must be a list of strings")
        try:
            config = cls(
                depth=int(scope.get("depth", 2)),
                max_issues=int(scope.get("max_issues", 100)),
                include_bugs=bool(scope.get("include_bugs", False)),
                link_types=tuple(item.strip() for item in link_types if item.strip()),
                filter_noise=bool(comments.get("filter_noise", True)),
                chunk_chars=int(retrieval.get("chunk_chars", 8000)),
                top_k=int(retrieval.get("top_k", 10)),
                query_terms=int(retrieval.get("query_terms", 12)),
                stopwords=tuple(item.casefold().strip() for item in stopwords if item.strip()),
                relative_threshold=float(retrieval.get("relative_threshold", 0.55)),
                min_matched_terms=int(retrieval.get("min_matched_terms", 2)),
                rrf_k=int(retrieval.get("rrf_k", 60)),
                anchor_weight=float(retrieval.get("anchor_weight", 1.5)),
                bm25_weight=float(retrieval.get("bm25_weight", 1.0)),
                dense_weight=float(retrieval.get("dense_weight", 1.0)),
                graph_weight=float(graph.get("weight", 0.15)),
                graph_seeds=int(graph.get("seeds", 5)),
                dense_enabled=bool(dense.get("enabled", False)),
                dense_model=str(dense.get("model", "")),
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid Jira configuration in {path}: {error}") from error
        if config.depth < 0 or config.max_issues < 1 or config.chunk_chars < 256:
            raise ValueError("Jira scope depth, issue cap, and chunk size are outside valid ranges")
        if config.top_k < 1 or config.query_terms < 1 or config.min_matched_terms < 1 or config.rrf_k < 1:
            raise ValueError("Jira retrieval counts must be positive integers")
        if not 0.0 <= config.relative_threshold <= 1.0:
            raise ValueError("Jira retrieval relative_threshold must be between 0 and 1")
        if min(config.anchor_weight, config.bm25_weight, config.dense_weight, config.graph_weight) < 0:
            raise ValueError("Jira retrieval weights cannot be negative")
        if config.graph_seeds < 1:
            raise ValueError("Jira graph seeds must be at least 1")
        return config

"""Provider abstraction and constrained note extraction."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Protocol

from capture_app.env import load_project_env


MAX_SOURCE_BATCH_CHARS = 48_000
SOURCE_MARKER_RE = re.compile(r"(?m)^\[SOURCE: [^\]\r\n]+\]$")


class JsonProvider(Protocol):
    name: str
    def complete_json(self, system: str, user: str) -> object: ...


@dataclass
class OpenRouterProvider:
    model: str | None = None
    name: str = "openrouter"

    def complete_json(self, system: str, user: str) -> object:
        # Lazy import keeps the default installation independent of the
        # optional remote provider and its dotenv integration.
        from capture_app.integrations.openrouter import chat_json
        return chat_json(system, user, model=self.model, temperature=0.1)


@dataclass
class LocalOpenAIProvider:
    url: str
    model: str
    api_key: str | None = None
    timeout: float = 120.0
    name: str = "local"

    def complete_json(self, system: str, user: str) -> object:
        import httpx
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = httpx.post(
            self.url, headers=headers,
            json={"model": self.model, "temperature": 0.1,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)


DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"
CLAUDE_MAX_TOKENS = 4000
CLAUDE_MAX_RETRIES = 3


@dataclass
class ClaudeProvider:
    """JSON provider backed by Anthropic's official Python SDK."""
    model: str = DEFAULT_CLAUDE_MODEL
    max_tokens: int = CLAUDE_MAX_TOKENS
    max_retries: int = CLAUDE_MAX_RETRIES
    name: str = "claude"

    def complete_json(self, system: str, user: str) -> object:
        load_project_env()
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "Claude provider requires ANTHROPIC_API_KEY. "
                "Add it to the project .env or the process environment."
            )

        try:
            import anthropic
        except ImportError as error:
            raise RuntimeError(
                "Claude provider requires the official 'anthropic' Python package. "
                "Install the project requirements."
            ) from error

        client = anthropic.Anthropic(api_key=api_key, max_retries=0)
        json_system = system.rstrip() + "\nReturn valid JSON only, without Markdown fences or commentary."
        retryable_errors = tuple(
            error_type for name in ("RateLimitError", "OverloadedError")
            if isinstance((error_type := getattr(anthropic, name, None)), type)
        )
        for attempt in range(self.max_retries + 1):
            try:
                response = client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=json_system,
                    messages=[{"role": "user", "content": user}],
                )
                break
            except Exception as error:
                status = getattr(error, "status_code", None)
                retryable = isinstance(error, retryable_errors) or status in {429, 529}
                if not retryable or attempt >= self.max_retries:
                    raise
                time.sleep(min(0.5 * (2 ** attempt), 8.0))

        content = getattr(response, "content", None)
        text = "\n".join(
            block.text for block in content or ()
            if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)
        ).strip()
        if not text:
            raise ValueError("Claude returned no usable text content.")
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            text = fenced.group(1)
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError(f"Claude returned invalid JSON: {error}") from error


class ExtractionError(ValueError):
    pass


def build_prompts(block, catalog) -> tuple[str, str]:
    system = """Extract concise knowledge entries from QC notes. Use only facts stated in the note block. Preserve uncertainty and language. Do not infer business rules or confirmation. A status can be confirmed only when the note explicitly records confirmation; otherwise use unverified. Every claim must have an evidence_quote copied verbatim from the block. Select target_ids only from the supplied catalog. target_mentions must be exact text present in the note. If something is unclear, add a question; do not invent a link. Return one JSON object only with keys entries and questions."""
    user = {
        "source": block.locator,
        "note_block": block.text,
        "allowed_entities": catalog.prompt_rows(),
        "required_entry_fields": {
            "type": "flow | business-rule | screen | role | test-data | known-issue | decision | glossary",
            "feature": "string", "title": "string", "claim": "string",
            "evidence_quote": "string", "status": "confirmed | unverified",
            "confirmed_by": ["string"], "last_verified": "ISO date or null",
            "target_ids": ["catalog id"], "target_mentions": ["verbatim note mention"],
            "open_questions": ["string"],
        },
        "response_shape": {"entries": [], "questions": []},
    }
    return system, json.dumps(user, ensure_ascii=False)


def validate_extraction(result: object, block, catalog) -> dict:
    if not isinstance(result, dict) or not isinstance(result.get("entries"), list):
        raise ExtractionError("Expected a JSON object with an entries array")
    allowed_types = {"flow", "business-rule", "screen", "role", "test-data",
                     "known-issue", "decision", "glossary"}
    validated = []
    for index, raw in enumerate(result["entries"], start=1):
        if not isinstance(raw, dict):
            raise ExtractionError(f"Entry {index} is not an object")
        for field in ("type", "feature", "title", "claim", "evidence_quote"):
            if not isinstance(raw.get(field), str) or not raw[field].strip():
                raise ExtractionError(f"Entry {index} requires non-empty {field}")
        if raw["type"] not in allowed_types:
            raise ExtractionError(f"Entry {index} has invalid type")
        if raw.get("status") not in {"confirmed", "unverified"}:
            raise ExtractionError(f"Entry {index} has invalid status")
        quote = raw["evidence_quote"].strip()
        if quote not in block.text:
            raise ExtractionError(f"Entry {index} evidence_quote is not verbatim note text")
        ids = raw.get("target_ids", [])
        mentions = raw.get("target_mentions", [])
        if not isinstance(ids, list) or not isinstance(mentions, list):
            raise ExtractionError(f"Entry {index} targets must be arrays")
        valid_ids = []
        target_questions = []
        for entity_id in ids:
            if not isinstance(entity_id, str) or entity_id not in catalog.by_id:
                target_questions.append(f"Unresolved or invalid target id: {entity_id}")
            elif entity_id not in valid_ids:
                valid_ids.append(entity_id)
        valid_mentions = []
        for mention in mentions:
            if not isinstance(mention, str) or not mention.strip():
                continue
            mention = mention.strip()
            if mention.casefold() not in block.text.casefold():
                target_questions.append(f"Target mention does not appear in note: {mention}")
            else:
                valid_mentions.append(mention)
        confirmed_by = raw.get("confirmed_by", [])
        if not isinstance(confirmed_by, list):
            confirmed_by = []
        confirmed_by = [str(person).strip() for person in confirmed_by
                        if str(person).strip()
                        and str(person).strip().casefold() in block.text.casefold()]
        status = raw["status"]
        raw_questions = raw.get("open_questions", [])
        open_questions = [str(q) for q in raw_questions] if isinstance(raw_questions, list) else []
        if status == "confirmed" and (
            not confirmed_by or any(person.casefold() not in block.text.casefold()
                                    for person in confirmed_by)
        ):
            status = "unverified"
            confirmed_by = []
            open_questions.append("Confirmation details were not found verbatim in the source note.")
        last_verified = raw.get("last_verified") if isinstance(raw.get("last_verified"), str) else None
        if last_verified and last_verified not in block.text:
            last_verified = None
        validated.append({
            "type": raw["type"], "feature": raw["feature"].strip(),
            "title": raw["title"].strip(), "claim": raw["claim"].strip(),
            "evidence_quote": quote,
            "status": status,
            "confirmed_by": confirmed_by,
            "last_verified": last_verified,
            "target_ids": valid_ids, "target_mentions": valid_mentions,
            "open_questions": open_questions,
            "validation_questions": target_questions,
        })
    questions = result.get("questions", [])
    if not isinstance(questions, list):
        questions = ["LLM questions field was not an array"]
    return {"entries": validated, "questions": [str(q) for q in questions]}


def extract(block, catalog, provider: JsonProvider) -> dict:
    system, user = build_prompts(block, catalog)
    try:
        result = provider.complete_json(system, user)
    except Exception as provider_error:
        raise ExtractionError(f"Provider call failed: {provider_error}") from provider_error
    try:
        return validate_extraction(result, block, catalog)
    except ExtractionError as validation_error:
        # One constrained repair call handles malformed/unsupported model
        # output. Network/provider failures are not repeated automatically.
        repair_system = "Return corrected JSON only. Ensure every evidence_quote is verbatim text and every target id is in the allowed entity catalog. Do not add facts."
        repair_user = json.dumps({"validation_error": str(validation_error), "source": block.locator,
                                  "note_block": block.text,
                                  "allowed_entities": catalog.prompt_rows()}, ensure_ascii=False)
        try:
            result = provider.complete_json(repair_system, repair_user)
            return validate_extraction(result, block, catalog)
        except Exception as retry_error:
            raise ExtractionError(f"Validation failed after one repair attempt: {retry_error}") from retry_error


def _marked_blocks(markdown: str, max_chars: int) -> list[str]:
    """Split combined content at source markers, then split oversized blocks without dropping text."""
    matches = list(SOURCE_MARKER_RE.finditer(markdown))
    if not matches:
        return [markdown] if markdown.strip() else []
    sections = []
    prefix = markdown[:matches[0].start()].strip()
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        section = markdown[match.start():end].strip()
        if index == 0 and prefix:
            section = prefix + "\n" + section
        if len(section) <= max_chars:
            sections.append(section)
            continue
        source_match = SOURCE_MARKER_RE.search(section)
        if source_match is None:
            sections.append(section)
            continue
        marker = source_match.group(0)
        preamble = section[:source_match.start()].strip()
        body = section[source_match.end():].lstrip("\n")
        chunks = []
        remaining = body
        first_chunk = True
        while remaining:
            capacity = max_chars - len(marker) - 1
            if first_chunk and preamble:
                capacity -= len(preamble) + 1
            capacity = max(1, capacity)
            if len(remaining) <= capacity:
                piece, remaining = remaining, ""
            else:
                split_at = remaining.rfind("\n\n", 0, capacity)
                if split_at <= 0:
                    split_at = capacity
                piece, remaining = remaining[:split_at], remaining[split_at:]
                remaining = remaining.lstrip("\n")
            heading = (preamble + "\n" if first_chunk and preamble else "")
            chunks.append(heading + marker + "\n" + piece)
            first_chunk = False
        sections.extend(chunks)
    batches, current = [], []
    current_size = 0
    for section in sections:
        if current and current_size + len(section) + 2 > max_chars:
            batches.append("\n\n".join(current))
            current, current_size = [], 0
        current.append(section)
        current_size += len(section) + 2
    if current:
        batches.append("\n\n".join(current))
    return batches


def _source_sections(markdown: str) -> dict[str, str]:
    matches = list(SOURCE_MARKER_RE.finditer(markdown))
    result = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        result[match.group(0)] = markdown[match.end():end]
    return result


def extract_sources(markdown: str, catalog, provider: JsonProvider,
                    max_chars: int = MAX_SOURCE_BATCH_CHARS) -> dict:
    """Extract supported knowledge items from source-marked combined input."""
    if max_chars < 256:
        raise ValueError("max_chars must be at least 256")
    if not markdown.strip():
        return {"items": [], "questions": [], "batches": 0, "rejected_items": 0}
    source_sections = _source_sections(markdown)
    batches = _marked_blocks(markdown, max_chars)
    entities = catalog.prompt_rows()
    system = (
        "Extract evidence-grounded test knowledge from the supplied source blocks. Return one JSON object "
        "with keys items and questions. Each item must have type (decision, business-rule, requirement, "
        "clarification, bug, open-question, or test-data), title, claim, evidence_quote copied exactly, and "
        "source_marker copied exactly including brackets. Bug items should include symptom, cause, and fix "
        "when present in evidence; do not invent missing values. Select target_ids only from allowed entities. "
        "Preserve uncertainty, do not infer confirmation, and return JSON only."
    )
    allowed_types = {"decision", "business-rule", "requirement", "clarification", "bug",
                     "open-question", "test-data"}
    items, questions, rejected = [], [], 0
    for batch in batches:
        user = json.dumps({"source_blocks": batch, "allowed_entities": entities,
                           "response_shape": {"items": [], "questions": []}}, ensure_ascii=False)
        try:
            result = provider.complete_json(system, user)
        except Exception as provider_error:
            raise ExtractionError(f"Provider call failed: {provider_error}") from provider_error
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ExtractionError("Expected a JSON object with an items array")
        questions.extend(str(question) for question in result.get("questions", [])
                         if isinstance(question, (str, int, float)))
        for raw in result["items"]:
            if not isinstance(raw, dict):
                rejected += 1
                continue
            quote = raw.get("evidence_quote")
            marker = raw.get("source_marker")
            item_type = raw.get("type")
            if (item_type not in allowed_types or not isinstance(quote, str) or not quote.strip()
                    or not isinstance(marker, str) or marker not in source_sections
                    or quote not in source_sections.get(marker, "") or quote not in batch):
                rejected += 1
                continue
            required_strings = ("title", "claim")
            if any(not isinstance(raw.get(field), str) or not raw[field].strip()
                   for field in required_strings):
                rejected += 1
                continue
            target_ids = raw.get("target_ids", [])
            if not isinstance(target_ids, list):
                target_ids = []
            clean_targets = [target for target in dict.fromkeys(target_ids)
                             if isinstance(target, str) and target in catalog.by_id]
            clean = {"type": item_type, "title": raw["title"].strip(),
                     "claim": raw["claim"].strip(), "evidence_quote": quote,
                     "source_marker": marker, "target_ids": clean_targets}
            for field in ("symptom", "cause", "fix", "status"):
                if isinstance(raw.get(field), str):
                    clean[field] = raw[field].strip()
            items.append(clean)
    return {"items": items, "questions": questions, "batches": len(batches),
            "rejected_items": rejected}


def provider_from_args(kind: str, model: str | None = None, local_url: str | None = None,
                       local_model: str | None = None,
                       claude_model: str | None = None) -> JsonProvider:
    load_project_env()
    if kind == "openrouter":
        return OpenRouterProvider(model=model or os.environ.get("OPENROUTER_MODEL"))
    if kind == "claude":
        return ClaudeProvider(model=claude_model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_CLAUDE_MODEL)
    if kind != "local":
        raise ValueError(f"Unsupported Knowledge LLM provider {kind!r}; use 'local', 'openrouter', or 'claude'.")
    url = local_url or os.environ.get("KNOWLEDGE_LOCAL_URL")
    local_model = local_model or os.environ.get("KNOWLEDGE_LOCAL_MODEL")
    if not url or not local_model:
        raise ValueError("Local provider requires --local-url and --local-model (or KNOWLEDGE_LOCAL_URL / KNOWLEDGE_LOCAL_MODEL)")
    return LocalOpenAIProvider(url=url, model=local_model,
                               api_key=os.environ.get("KNOWLEDGE_LOCAL_API_KEY"))

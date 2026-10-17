"""Provider abstraction and constrained note extraction."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol

from capture_app.env import load_project_env


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


def provider_from_args(kind: str, model: str | None = None, local_url: str | None = None,
                       local_model: str | None = None) -> JsonProvider:
    load_project_env()
    if kind == "openrouter":
        return OpenRouterProvider(model=model)
    url = local_url or os.environ.get("KNOWLEDGE_LOCAL_URL")
    local_model = local_model or os.environ.get("KNOWLEDGE_LOCAL_MODEL")
    if not url or not local_model:
        raise ValueError("Local provider requires --local-url and --local-model (or KNOWLEDGE_LOCAL_URL / KNOWLEDGE_LOCAL_MODEL)")
    return LocalOpenAIProvider(url=url, model=local_model,
                               api_key=os.environ.get("KNOWLEDGE_LOCAL_API_KEY"))

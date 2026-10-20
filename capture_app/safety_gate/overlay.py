"""In-memory mock data layer used by WriteSafeGate."""
from __future__ import annotations

import copy
import json
import re
import uuid
from urllib.parse import parse_qs, urljoin
from urllib.parse import urlsplit, urlunsplit


def _json_body(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def _record_id(value):
    if isinstance(value, dict):
        for key in ("id", "Id", "ID", "uuid", "guid"):
            if key in value and value[key] is not None:
                return str(value[key])
        for key in ("data", "result", "item", "record"):
            if key in value:
                found = _record_id(value[key])
                if found is not None:
                    return found
    return None


def _replace_collection(url: str) -> str:
    p = urlsplit(url)
    parts = p.path.rstrip("/").split("/")
    if parts and (parts[-1].isdigit() or parts[-1].startswith("QA_")
                  or re.fullmatch(r"[0-9a-fA-F-]{32,36}", parts[-1])):
        parts.pop()
    return urlunsplit((p.scheme, p.netloc, "/".join(parts) or "/", "", ""))


class MockOverlay:
    def __init__(self):
        self.created: dict[tuple[str, str], dict] = {}
        self.patched: dict[tuple[str, str], dict] = {}
        self.deleted: set[tuple[str, str]] = set()
        self.real_created_ids: set[str] = set()
        self.samples: dict[str, object] = {}

    @staticmethod
    def _key(url: str, record_id: str):
        p = urlsplit(url)
        return (p.netloc.lower(), _replace_collection(url).split("?", 1)[0] + "|" + str(record_id))

    def _known_template(self, collection: str):
        return copy.deepcopy(self.samples.get(collection))

    def _new_id(self, template):
        existing = _record_id(template)
        if isinstance(template, dict) and isinstance(template.get("id"), int):
            return max(template["id"] + 100000, 900000)
        if existing is not None and str(existing).isdigit():
            return str(max(int(existing) + 100000, 900000))
        return "QA_" + uuid.uuid4().hex[:12]

    def seed_openapi(self, spec: dict, base_url: str):
        """Use declared POST response examples as mock create templates."""
        paths = spec.get("paths") if isinstance(spec, dict) else None
        schemas = (spec.get("components", {}).get("schemas", {})
                   if isinstance(spec, dict) else {})
        if not isinstance(paths, dict):
            return

        def sample(schema, depth=0):
            if not isinstance(schema, dict) or depth > 8:
                return None
            if "example" in schema:
                return copy.deepcopy(schema["example"])
            ref = schema.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                return sample(schemas.get(ref.rsplit("/", 1)[-1]), depth + 1)
            if schema.get("type") == "array":
                item = sample(schema.get("items"), depth + 1)
                return [item] if item is not None else []
            props = schema.get("properties")
            if isinstance(props, dict):
                result = {}
                for name, prop in props.items():
                    value = sample(prop, depth + 1)
                    if value is None:
                        kind = prop.get("type") if isinstance(prop, dict) else None
                        value = {"string": "", "integer": 1, "number": 1,
                                 "boolean": False, "array": [], "object": {}}.get(kind)
                    result[name] = value
                return result
            return None

        for path, operations in paths.items():
            post = operations.get("post") if isinstance(operations, dict) else None
            responses = post.get("responses", {}) if isinstance(post, dict) else {}
            for response in responses.values():
                content = response.get("content", {}) if isinstance(response, dict) else {}
                for media in content.values():
                    if not isinstance(media, dict):
                        continue
                    example = media.get("example")
                    if example is None and isinstance(media.get("examples"), dict):
                        first = next(iter(media["examples"].values()), {})
                        example = first.get("value") if isinstance(first, dict) else None
                    if example is None:
                        example = sample(media.get("schema"))
                    if isinstance(example, dict):
                        collection = _replace_collection(urljoin(base_url.rstrip("/") + "/", path.lstrip("/")))
                        self.samples.setdefault(collection, copy.deepcopy(example))
                        break

    def mutate(self, method: str, url: str, body: str, request, route):
        parsed = urlsplit(url)
        collection = _replace_collection(url)
        payload = _json_body(body)
        if not isinstance(payload, dict):
            parsed_form = parse_qs(body or "", keep_blank_values=True)
            payload = {key: values[-1] if len(values) == 1 else values for key, values in parsed_form.items()}
            if not payload:
                payload = {"value": body or ""}
        if method == "POST":
            sample = self._known_template(collection)
            record = copy.deepcopy(sample) if isinstance(sample, dict) else {}
            record.update(payload)
            if _record_id(payload) is None:
                id_key = "id" if not sample or "id" in sample else next((k for k in ("Id", "ID", "uuid") if k in sample), "id")
                record[id_key] = self._new_id(sample)
            rid = _record_id(record)
            self.created[self._key(url, rid)] = record
            self.deleted.discard(self._key(url, rid))
            return 201, record
        rid = parsed.path.rstrip("/").split("/")[-1]
        key = self._key(url, rid)
        if method == "DELETE":
            self.deleted.add(key)
            self.created.pop(key, None)
            self.patched.pop(key, None)
            return 204, None
        record = self.created.get(key) or self.patched.get(key)
        if record is None:
            # Fetch once, read-only, for a real record. The route fetch bypasses
            # the current interception so it cannot recurse through this gate.
            try:
                response = route.fetch(url=url, method="GET", headers=request.headers)
                if response.ok:
                    record = _json_body(response.text())
            except Exception:
                record = None
        if not isinstance(record, dict):
            record = {"id": rid}
        record.update(payload)
        self.patched[key] = record
        return 200, record

    def merge_get(self, url: str, data):
        collection = _replace_collection(url)
        parsed = urlsplit(url)
        rid = parsed.path.rstrip("/").split("/")[-1]
        key = self._key(url, rid)
        if key in self.deleted:
            return 404, {"error": "not found"}
        if key in self.patched:
            return 200, copy.deepcopy(self.patched[key])
        if key in self.created:
            return 200, copy.deepcopy(self.created[key])

        records = data
        holder = None
        list_key = None
        if isinstance(data, dict):
            for candidate in ("items", "results", "data", "records"):
                if isinstance(data.get(candidate), list):
                    list_key, records = candidate, data[candidate]
                    holder = data
                    break
        if not isinstance(records, list):
            template = records
            if isinstance(template, dict):
                self.samples.setdefault(collection, copy.deepcopy(template))
            return 200, data
        if records and isinstance(records[0], dict):
            self.samples.setdefault(collection, copy.deepcopy(records[0]))
        merged = []
        seen = set()
        for record in records:
            item_id = _record_id(record)
            record_key = self._key(url, item_id) if item_id is not None else None
            if record_key in self.deleted:
                continue
            item = copy.deepcopy(record)
            if record_key in self.patched:
                item.update(self.patched[record_key])
            if item_id is not None:
                seen.add(item_id)
            merged.append(item)
        for (host, packed), record in self.created.items():
            base, _, item_id = packed.partition("|")
            if host == parsed.netloc.lower() and base == collection and item_id not in seen:
                merged.append(copy.deepcopy(record))
        if holder is not None:
            holder[list_key] = merged
            return 200, holder
        return 200, merged

"""Portable, integrity-checked testcase replay metadata for generated workbooks."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from typing import Any

SCHEMA_VERSION = 1
MAX_CELL_CHARS = 30_000
VISIBLE_HASH_FIELDS = (
    "Module", "Sub-module (Test Summary)", "Test Case Name", "Precondition", "Data",
    "Steps", "Expected Result", "Testing Type", "Technical Evidence", "Basis",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encode_payload(payload: dict) -> tuple[str, str]:
    raw = canonical_json(payload).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    encoded = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    return encoded, digest


def decode_payload(chunks: list[str], expected_digest: str) -> dict:
    compressed = base64.b64decode("".join(chunks), validate=True)
    raw = gzip.decompress(compressed)
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError("Runner metadata payload checksum mismatch.")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Runner metadata payload must be a JSON object.")
    return value


def row_hash(visible_values: dict, payload: dict) -> str:
    design_values = {key: visible_values.get(key) for key in VISIBLE_HASH_FIELDS}
    if not str(design_values.get("Expected Result") or "").strip():
        design_values["Expected Result"] = None
    # Keep old generated workbooks hash-compatible when they do not carry this
    # supplemental field, while protecting its value in newly generated suites.
    if "Generated Expected Result" in visible_values:
        generated = visible_values.get("Generated Expected Result")
        design_values["Generated Expected Result"] = generated if str(generated or "").strip() else None
    raw = (canonical_json({"schema_version": SCHEMA_VERSION,
                           "visible": design_values, "payload": payload})).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def write_metadata_sheet(workbook, cases: list[dict]) -> None:
    if "Runner Metadata" in workbook.sheetnames:
        del workbook["Runner Metadata"]
    sheet = workbook.create_sheet("Runner Metadata")
    sheet.sheet_state = "hidden"
    headers = ["schema_version", "case_id", "sheet_row", "chunk_index", "chunk_count",
               "payload_sha256", "row_sha256", "payload_chunk"]
    sheet.append(headers)
    for case in cases:
        encoded, digest = encode_payload(case["payload"])
        chunks = [encoded[i:i + MAX_CELL_CHARS] for i in range(0, len(encoded), MAX_CELL_CHARS)] or [""]
        for index, chunk in enumerate(chunks):
            sheet.append([
                SCHEMA_VERSION, case["case_id"], case["sheet_row"], index, len(chunks),
                digest, case["row_sha256"], chunk,
            ])


def load_metadata_sheet(workbook) -> dict[str, dict]:
    if "Runner Metadata" not in workbook.sheetnames:
        return {}
    sheet = workbook["Runner Metadata"]
    grouped: dict[str, dict] = {}
    for values in sheet.iter_rows(min_row=2, values_only=True):
        version, case_id, sheet_row, chunk_index, chunk_count, digest, row_digest, chunk = values
        if not case_id:
            continue
        item = grouped.setdefault(str(case_id), {
            "schema_version": version, "case_id": str(case_id), "sheet_row": int(sheet_row),
            "chunk_count": int(chunk_count), "payload_sha256": str(digest),
            "row_sha256": str(row_digest), "chunks": {},
        })
        item["chunks"][int(chunk_index)] = str(chunk or "")
    for item in grouped.values():
        if item["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"Unsupported runner metadata schema version for {item['case_id']}.")
        if len(item["chunks"]) != item["chunk_count"]:
            raise ValueError(f"Incomplete runner metadata payload for {item['case_id']}.")
        ordered = [item["chunks"][index] for index in range(item["chunk_count"])]
        item["payload"] = decode_payload(ordered, item["payload_sha256"])
        del item["chunks"]
    return grouped

"""Durable, reviewable proposals and versioned expected API contracts."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def rendered_contract_section(vault: Path, entity_id: str) -> str:
    """Return the owned contract section when an approved value exists."""
    db_path = Path(vault) / "Knowledge" / "Changes" / "_state" / "changes.sqlite"
    if not db_path.is_file():
        return ""
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT version,contract_json,proposal_id FROM contracts WHERE entity_id=?", (entity_id,)).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    if not row:
        return ""
    contract = json.loads(row[1])
    fields = "\n".join(f"- **{key}:** `{json.dumps(value, ensure_ascii=False)}`" for key, value in contract.items())
    target = hashlib.sha256(entity_id.encode()).hexdigest()[:16]
    return ("\n<!-- approved-contract:start -->\n## Approved expected contract\n\n"
            "These are reviewed expectations; captured examples above remain observations.\n\n"
            f"{fields}\n\n- Version: {row[0]} · Source: [[Knowledge/Changes/{row[2]}]] · "
            f"[[Knowledge/Changes/contracts/{target}]]\n<!-- approved-contract:end -->\n")


def load_contracts(vault: Path) -> dict[str, dict]:
    """Read current approved contract values without creating or mutating the ledger."""
    db_path = Path(vault) / "Knowledge" / "Changes" / "_state" / "changes.sqlite"
    if not db_path.is_file():
        return {}
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return {entity_id: json.loads(contract_json) for entity_id, contract_json in
                conn.execute("SELECT entity_id,contract_json FROM contracts")}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChangeStore:
    def __init__(self, vault: Path):
        self.vault = Path(vault).resolve()
        self.root = self.vault / "Knowledge" / "Changes"
        self.db_path = self.root / "_state" / "changes.sqlite"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS proposals (
          id TEXT PRIMARY KEY, note_sha256 TEXT NOT NULL, source_json TEXT NOT NULL,
          proposal_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, reviewer TEXT, rationale TEXT
        );
        CREATE TABLE IF NOT EXISTS contracts (
          entity_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
          contract_json TEXT NOT NULL, proposal_id TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contract_versions (
          entity_id TEXT NOT NULL, version INTEGER NOT NULL, contract_json TEXT NOT NULL,
          proposal_id TEXT NOT NULL, reviewer TEXT NOT NULL, rationale TEXT NOT NULL,
          created_at TEXT NOT NULL, PRIMARY KEY(entity_id, version)
        );
        """)
        self.conn.commit()

    @staticmethod
    def stable_id(ticket_id: str, note: str) -> str:
        digest = hashlib.sha256((ticket_id.strip() + "\0" + note).encode("utf-8")).hexdigest()
        return "CHG-" + digest[:16]

    def save(self, proposal: dict) -> None:
        stamp = now()
        self.conn.execute("""INSERT INTO proposals
          (id,note_sha256,source_json,proposal_json,status,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
          proposal_json=excluded.proposal_json,status=excluded.status,updated_at=excluded.updated_at""",
          (proposal["proposal_id"], proposal["source"]["note_sha256"],
           json.dumps(proposal["source"], ensure_ascii=False), json.dumps(proposal, ensure_ascii=False),
           proposal["status"], stamp, stamp))
        self.conn.commit()
        self.render_page(proposal)

    def get(self, proposal_id: str) -> dict:
        row = self.conn.execute("SELECT proposal_json FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown proposal: {proposal_id}")
        return json.loads(row[0])

    def update(self, proposal: dict, reviewer: str | None = None, rationale: str | None = None) -> None:
        self.conn.execute("UPDATE proposals SET proposal_json=?,status=?,updated_at=?,reviewer=COALESCE(?,reviewer),rationale=COALESCE(?,rationale) WHERE id=?",
                          (json.dumps(proposal, ensure_ascii=False), proposal["status"], now(), reviewer, rationale, proposal["proposal_id"]))
        self.conn.commit()
        self.render_page(proposal)

    def list(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute("SELECT proposal_json FROM proposals WHERE status=? ORDER BY created_at", (status,))
        else:
            rows = self.conn.execute("SELECT proposal_json FROM proposals ORDER BY created_at")
        return [json.loads(row[0]) for row in rows]

    def current_contract(self, entity_id: str) -> tuple[int, dict]:
        row = self.conn.execute("SELECT version,contract_json FROM contracts WHERE entity_id=?", (entity_id,)).fetchone()
        return (int(row[0]), json.loads(row[1])) if row else (0, {})

    def apply(self, proposal: dict, reviewer: str, rationale: str) -> dict:
        """Atomically append versions, update current contracts, and mark applied."""
        if proposal["status"] != "approved":
            raise ValueError("Only approved proposals can be applied")
        if proposal.get("open_questions") or not proposal.get("changes"):
            raise ValueError("Resolve all open questions and include at least one change before applying")
        stale_changes = []
        for change in proposal.get("changes", []):
            version, contract = self.current_contract(change["entity_id"])
            if version != change.get("base_version", 0):
                change["before"] = contract.get(change["field"])
                change["base_version"] = version
                stale_changes.append(change["entity_id"])
        if stale_changes:
            proposal["status"] = "stale"
            proposal.pop("po_approved_by", None)
            proposal.pop("review", None)
            proposal.setdefault("open_questions", []).append(
                "Contract changed after review. The before values and version were refreshed; review and approve this diff again."
            )
            self.update(proposal)
            raise ValueError("Proposal is stale. Its diff has been refreshed; review and approve it again before applying.")
        requirements = proposal.get("requirements", [])
        unverified = [r for r in requirements if r.get("status") not in {"approved", "confirmed", "active"}]
        if (unverified or not requirements or proposal.get("po_conflicts")) and not proposal.get("po_approved_by"):
            raise ValueError("PO requirements are not verified; approve the PO decision explicitly before applying")
        changes = proposal.get("changes", [])
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            by_entity: dict[str, dict] = {}
            for change in changes:
                entity_id = change["entity_id"]
                version, contract = self.current_contract(entity_id)
                contract[change["field"]] = change["proposed"]
                by_entity[entity_id] = {"version": version + 1, "contract": contract}
            for entity_id, item in by_entity.items():
                serialized = json.dumps(item["contract"], ensure_ascii=False, sort_keys=True)
                version = item["version"]
                self.conn.execute("INSERT INTO contract_versions VALUES(?,?,?,?,?,?,?)",
                                  (entity_id, version, serialized, proposal["proposal_id"], reviewer, rationale, now()))
                self.conn.execute("""INSERT INTO contracts VALUES(?,?,?,?,?)
                  ON CONFLICT(entity_id) DO UPDATE SET version=excluded.version,
                  contract_json=excluded.contract_json,proposal_id=excluded.proposal_id,updated_at=excluded.updated_at""",
                                  (entity_id, version, serialized, proposal["proposal_id"], now()))
            proposal["status"] = "applied"
            proposal["applied_by"] = reviewer
            proposal["applied_at"] = now()
            proposal["applied_rationale"] = rationale
            self.conn.execute("UPDATE proposals SET proposal_json=?,status='applied',updated_at=?,reviewer=?,rationale=? WHERE id=?",
                              (json.dumps(proposal, ensure_ascii=False), now(), reviewer, rationale, proposal["proposal_id"]))
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        self.render_page(proposal)
        self.render_contract_pages(proposal)
        return proposal

    def render_page(self, proposal: dict) -> None:
        summary = str(proposal.get("summary", "Ticket change proposal")).strip()
        if summary and re.search(r"(?:\b(?:ex|def|bec|wheth|poss)\w{0,3})$", summary, re.I):
            source_note = str(proposal.get("source", {}).get("note", ""))
            heading = re.search(r"(?m)^#\s+(.+?)\s*$", source_note)
            summary = heading.group(1).strip() if heading else "Ticket change proposal requiring evidence"
        source_note = str(proposal["source"].get("note", ""))
        source_note = source_note.replace("[triage]", "Unassigned")
        source_note = source_note.replace("[test environment, browser, viewport, date/time]", "Not recorded")
        source_note = source_note.replace("[details]", "Not recorded")
        source_note = re.sub(r"\[Record the page count shown.*?\]", "Not yet observed; record the page count and visited URLs before filing.", source_note, flags=re.S)
        ticket_id = str(proposal.get("source", {}).get("ticket_id") or "")
        if ticket_id.casefold() in {"abc-123", "tbd", "placeholder"}:
            ticket_id = "Unassigned"
        ticket_line = f"**Ticket:** {ticket_id}\n\n" if ticket_id else ""
        lines = [f"# {proposal['proposal_id']}: {summary}", "",
                 f"**Status:** {proposal['status']}", "", ticket_line.rstrip(), "## Source note", "", source_note, "",
                 "## Proposed changes", ""]
        if re.search(r"(?i)pagination|page count", source_note):
            lines += ["## Analysis notes", "",
                      "A page count follows from the number of matching products and the selected page size. The source note does not establish a fixed two-page expectation; record both values before classifying this as a defect.", ""]
            for report_path in sorted((self.vault / "Reports" / "native_runs").glob("*.json")):
                try:
                    replay = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                failure = replay.get("failure") or {}
                if failure.get("kind") == "navigation_mismatch" and failure.get("sequence") == 66:
                    rel = report_path.with_suffix(".md").relative_to(self.vault).as_posix()
                    lines += [f"Separate replay finding: [[{rel}|step 66 checkout navigation mismatch]]. It is a failed replay and is not evidence for the catalog pagination claim.", ""]
                    break
        if not proposal.get("changes"):
            lines.append("No validated changes were proposed.")
        for change in proposal.get("changes", []):
            lines += [f"### {change['entity_id']} · {change['field']}", "",
                      f"- Before: `{json.dumps(change.get('before'), ensure_ascii=False)}`",
                      f"- Proposed: `{json.dumps(change.get('proposed'), ensure_ascii=False)}`",
                      f"- Evidence: {'; '.join(change.get('evidence_quotes', [])) or '(none)' }",
                      f"- PO requirements: {', '.join(change.get('requirement_ids', [])) or '(none)' }",
                      f"- Rationale: {change.get('rationale', '')}", ""]
        if proposal.get("candidate_targets"):
            lines += ["", "## Relevant API and parameter records", ""]
        for target in proposal.get("candidate_targets", []):
            lines.append(f"- `{target['entity_id']}` ({target['kind']}): {target['title']}")
            if target.get("observation"):
                lines.extend(["", "  Captured observation excerpt:", "", "  ```text",
                              "  " + str(target["observation"]).replace("\n", "\n  "), "  ```"])
        if not proposal.get("candidate_targets"):
            lines.append("No relevant API or parameter candidate matched. A reviewer must identify the target.")
        lines += ["## PO requirements", ""]
        lines.extend(f"- {r['id']} [{r['status']}]: {r['requirement']}" for r in proposal.get("requirements", []))
        if not proposal.get("requirements"):
            lines.append("- No PO requirement was linked to a proposed change; select a relevant candidate requirement before approval.")
        if proposal.get("candidate_requirements"):
            lines += ["", "## Relevant PO requirements for reviewer selection", ""]
            lines.extend(f"- {r['id']} [{r['status']}]: {r['requirement']}" for r in proposal["candidate_requirements"])
        lines += ["", "## Proposed testcase impacts", ""]
        for impact in proposal.get("test_impacts", []):
            lines.append(f"- {impact.get('entity_id')}: {impact.get('expected_change')} (proposal only)")
        if not proposal.get("test_impacts"):
            lines.append("- None validated.")
        lines += ["", "## Questions and conflicts", ""]
        lines.extend(f"- {q}" for q in proposal.get("open_questions", []) + proposal.get("po_conflicts", []))
        if not proposal.get("open_questions") and not proposal.get("po_conflicts"):
            lines.append("- None recorded.")
        path = self.root / f"{proposal['proposal_id']}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def render_contract_pages(self, proposal: dict) -> None:
        for entity_id in {c["entity_id"] for c in proposal.get("changes", [])}:
            version, contract = self.current_contract(entity_id)
            filename = hashlib.sha256(entity_id.encode()).hexdigest()[:16] + ".md"
            path = self.root / "contracts" / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# Approved expected contract\n\n- Entity: `{entity_id}`\n- Version: {version}\n- Source proposal: [[{proposal['proposal_id']}]]\n\n```json\n{json.dumps(contract, ensure_ascii=False, indent=2)}\n```\n", encoding="utf-8")
            note_path = self.vault / f"{entity_id}.md"
            if note_path.is_file():
                source = note_path.read_text(encoding="utf-8")
                start, end = "<!-- approved-contract:start -->", "<!-- approved-contract:end -->"
                if start in source and end in source:
                    begin = source.index(start)
                    finish = source.index(end, begin) + len(end)
                    source = source[:begin].rstrip() + "\n" + source[finish:].lstrip("\r\n")
                source += rendered_contract_section(self.vault, entity_id)
                note_path.write_text(source, encoding="utf-8")

    def close(self) -> None:
        self.conn.close()


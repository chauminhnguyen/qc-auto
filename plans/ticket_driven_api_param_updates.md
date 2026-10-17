# Plan: Ticket-Driven API and Parameter Updates

## Goal

Let a user submit a short ticket or error note in ordinary language. The LLM uses that note, the captured API/parameter catalog, and the active PO requirements to identify affected endpoints and parameters, then prepares a traceable proposed update for review.

The first version updates the vault's documented API/parameter contract and linked test expectations. It does not change a running application or its server implementation. Captured requests and responses remain evidence of what was observed; they are not silently rewritten to match a ticket.

## Existing foundation

- `capture_app/storage/vault.py` stores captured endpoints in `apis`, parameter examples and API references in `params`, and individual observations in `session_api_events`. It renders API and parameter Markdown from those records.
- `capture_app/knowledge/` already accepts raw notes, asks an LLM to extract claims, matches targets against a catalog, and writes reviewable drafts. Its reader opens the capture index read-only; its review step prom otes Knowledge notes but does not update APIs or Params.
- `ObsidianVault/BRDs/entries/` contains distilled PO requirements. The active requirement/status conventions must be followed when assembling context.
- Therefore, extend the separate Knowledge/review workflow with a proposal-and-apply layer. Do not let LLM output write into the capture tables or generated capture notes directly.

## Proposed user flow

1. User submits a note, for example: “Checkout fails when the state field is omitted. PO says state is optional for countries that do not use states; the generated request still sends `StateProvinceId=0`.” A ticket ID, reporter, environment, and links are optional metadata.
2. Intake stores the original note and its hash/source metadata. The text is treated as untrusted evidence, not as instructions to the model or the application.
3. Retrieval supplies the LLM with only relevant records: likely API/parameter entries and aliases, linked captures/flows, applicable active PO requirements, and any conflicting or superseded requirement decisions.
4. The model returns a structured proposal: issue summary, endpoint targets, parameter changes, evidence excerpts, PO requirement IDs, reasoning, confidence, unresolved questions, and affected tests. It must cite exact catalog IDs and requirement IDs; it cannot invent a target from free text.
5. Deterministic validation checks schema, target existence and type, quoted-evidence provenance, allowed fields, PO traceability, and current record versions. Unresolved or ambiguous targets and PO conflicts remain blocked drafts.
6. A reviewer sees a before/after diff, evidence, PO trace, capture samples, and test impact. They can accept, edit, reject, or ask for clarification.
7. On acceptance, persist a versioned contract change and update the generated API/Params documentation and linked test expectations atomically. Retain the original capture data and prior contract version. Record reviewer, timestamp, source ticket, and rationale.
8. Reports show pending proposals, approved changes, conflicts, and requirements with no mapped API/parameter coverage.

## Authority and safety rules

- Active PO requirements are the expected behavior source. A ticket can report a defect or propose a PO change, but it cannot silently override a confirmed requirement. If the ticket conflicts with an active PO requirement, mark `po_conflict` and require an explicit PO decision or a linked superseding requirement.
- Captures are observational evidence. Preserve method, URL, request/response examples, status, timestamps, and session provenance as captured. Store expected contract values separately from observed values.
- Distinguish a correction to documentation/test expectations from a change to production application code. Production changes remain outside this workflow and require an implementation ticket/repository.
- Never make a live API call or replay a captured mutating request as part of note analysis or proposal generation.
- Do not apply model-generated changes automatically. Human approval is required before updating canonical contract records or generated notes.
- If target identity, requirement applicability, or expected behavior is unclear, ask a question and leave the proposal unapplied.

## Proposal shape

Use JSON validated into a typed internal model. Suggested fields:

```json
{
  "proposal_id": "generated-stable-id",
  "source": {"ticket_id": "optional", "note": "...", "note_sha256": "..."},
  "summary": "...",
  "classification": "defect | requirement_change | documentation_gap | unknown",
  "targets": [
    {"entity_id": "catalog API or Param ID", "kind": "api | param", "change": "..."}
  ],
  "changes": [
    {"entity_id": "...", "field": "...", "before": "...", "proposed": "...",
     "evidence_quotes": ["verbatim text from note or linked record"],
     "requirement_ids": ["..."], "rationale": "..."}
  ],
  "test_impacts": [{"case_id": "...", "expected_change": "..."}],
  "open_questions": [],
  "confidence": 0.0,
  "status": "draft"
}
```

Limit `field` values to an explicit allowlist. For API contract proposals, initial fields can include expected method/path/query/body/header behavior and expected response/status. For parameters, include location, data type, requiredness, null/empty semantics, validation/constraints, default, and safe example. Keep observed examples and proposed expected values in separate sections.

## Implementation phases

### Phase 1: Define the contract and source links

1. Agree on the first supported change fields and the PO requirement status that counts as active.
2. Extend the entity catalog/context builder to return bounded details for API and parameter entities, including captured API references and safe observation summaries, plus linked BRD requirements and their status/source IDs.
3. Add an explicit mapping format from API/parameter IDs to PO requirement IDs if implicit links in Markdown are not reliable enough. Flag missing/ambiguous links instead of guessing.
4. Define and document the proposal JSON schema, statuses (`draft`, `needs_info`, `po_conflict`, `ready_for_review`, `approved`, `rejected`, `applied`, `stale`), and allowed patch fields.

### Phase 2: Intake, retrieval, and proposal generation

1. Add a ticket-note intake command alongside `capture_app.knowledge.cli`, reusing provider configuration and note discovery where useful.
2. Persist each source note and proposal under a dedicated `Knowledge/Changes/` area with a small SQLite ledger for stable IDs, status, source hash, target IDs, reviewer decisions, and applied version.
3. Retrieve context deterministically before the LLM call. Require citations to supplied entity and requirement IDs; prohibit unsupported claims and direct file/DB edits in the model contract.
4. Validate every proposed change, quote, target, requirement, status, and field in Python. Resolve fuzzy matches only as suggestions; require reviewer selection when more than one endpoint/parameter is plausible.
5. Generate a readable review page containing the note, matched evidence, PO trace, proposed diff, conflicts, and questions.

### Phase 3: Review and apply

1. Add review commands for listing, inspecting, accepting/editing, rejecting, and requesting information on proposals. Record reviewer identity and rationale.
2. Store approved expected contracts in separate versioned tables or files; do not repurpose `apis`/`params` capture columns as expected behavior.
3. Render API and parameter notes with clearly separate “Captured observations” and “Approved expected contract” sections, or link to the approved contract page. Avoid overwriting hand-maintained or capture-generated content outside owned sections.
4. Apply the database/file update transactionally: verify the source and target versions have not changed since review, write the approved contract, render notes, and record the applied proposal. If a write fails, leave the proposal unapplied and recoverable.
5. Preserve previous contract versions and allow a reviewer to supersede/revert an applied version with an auditable action.

### Phase 4: Test impact and reporting

1. Link affected testcase definitions to API/parameter IDs and PO requirement IDs where possible.
2. Update the BRD testcase generator so requirement IDs, BRD-entry IDs, priorities, and source paths are stored in traceability metadata, never embedded into Data or other tester-facing prose.
3. Generate BRD-linked testcase names, preconditions, data, steps, and expected results as natural, concise instructions. Use approved requirement wording and verified context; expose unsupported setup or ambiguous behavior as a question instead of inventing an executable scenario.
4. Make the Reader Summary use natural-language values for every tester-facing testcase field, including Data. Keep technical provenance and detailed references in dedicated columns and Technical Details.
5. For approved API/parameter changes, generate a proposed testcase delta. Require deterministic expected results backed by a PO requirement or verified observation; otherwise label the case `needs exploration`.
6. Add reports for ticket-to-API/parameter traceability, PO coverage, unresolved targets, conflicts, and stale proposals.
7. Document that this process updates the capture vault's expected contract and test documentation; application code fixes are separate work.

## Likely code locations

- Reuse `capture_app/knowledge/llm.py` provider abstraction, but add a separate proposal prompt and schema rather than changing the existing QC-knowledge extractor's allowed entry types.
- Extend `capture_app/knowledge/vault_reader.py` or add a contract-context module to retrieve API/parameter detail and requirement links without constructing `VaultWriter` during analysis.
- Add proposal/review/apply logic in new modules such as `capture_app/knowledge/changes.py` and `capture_app/knowledge/change_store.py`.
- Extend `capture_app/knowledge/cli.py` with focused commands such as `propose`, `list-changes`, `show-change`, `review-change`, and `apply-change`.
- Extend `capture_app/storage/vault.py` only for rendering approved contract sections after the contract store is defined; keep `_upsert_param()` as a capture-observation path.
- Update `capture_app/knowledge/README.md` and `RUNBOOK.md` with review workflow, provider/data handling, status semantics, and the boundary between contract documentation and application implementation.

## MVP acceptance criteria

- A plain-language note can create a durable draft that cites one or more existing API/parameter IDs, or clearly reports that target resolution needs a human.
- The draft shows relevant active PO requirements and flags direct conflicts rather than changing the expected behavior silently.
- The LLM cannot directly write to the capture index or generated API/Params files.
- A reviewer can inspect a before/after diff and approve, reject, or request information; only an approved proposal can update the separate expected contract.
- Captured request/response evidence remains unchanged and visibly distinct from approved expectations.
- Every applied change links back to its source note/ticket, evidence, PO requirement, reviewer, and previous version.
- Applying a stale proposal is rejected with a refreshed diff.
- Ambiguous, unsupported, or conflicting changes remain unapplied and include a clear reason/question.
- The expected result is a faithful, testable statement of observable behavior when the requirement supports one. Broad/unresolved requirements are surfaced for test design rather than padded with generic steps or invented outcomes.

## First vertical slice

Start with parameter contract changes for one endpoint family: required/optional state or address fields in checkout. Demonstrate intake → endpoint/parameter matching → active PO requirement trace → conflict/uncertainty handling → reviewer diff → approved versioned contract update. Then expand to payload shape, validation/default behavior, endpoint method/path, and response expectations. This exercises the most important safety boundary before allowing broader endpoint changes.

## Decisions still needed before implementation

- Are these “API/parameter updates” meant to update only this repository's API catalog and test contract, or should the workflow also create/change application-server code? This plan treats server implementation as separate work.
- Which ticket source and input channel should be supported first: pasted note/file, existing ticket export, or a tracker integration?
- Which BRD status values count as active, and who is authorized to resolve a ticket-versus-PO conflict?
- Should approved contracts live in the capture SQLite index or a separate contract database? A separate store is recommended to isolate observed capture data and simplify rollback.

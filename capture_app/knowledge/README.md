# QC Knowledge layer

This package ingests UTF-8 QC notes into `ObsidianVault/Knowledge/`. It reads
the capture index through a SQLite `mode=ro` connection and does not import or
construct `VaultWriter`. Generated entries, its processing ledger, and reports
are kept under `Knowledge/`; raw note files are read only.

## Commands

From the repository root:

```powershell
python -m capture_app.knowledge.cli ingest --vault .\ObsidianVault --notes .\Notes-raw --provider local --local-url http://127.0.0.1:1234/v1/chat/completions --local-model your-model
python -m capture_app.knowledge.cli review --vault .\ObsidianVault
python -m capture_app.knowledge.cli report --vault .\ObsidianVault
```

The local provider is selected explicitly and expects an OpenAI-compatible
chat-completions endpoint. To use OpenRouter, select `--provider openrouter`
and configure the existing `OPENROUTER_API_KEY`; note contents will be sent to
that remote provider. The input is a folder of `.md`/`.txt` files or one such
file. Headings (`##` / `###`) group a heading and its paragraphs into a block.
For stable identity when editing notes, add `<!-- knowledge-id: cart-rule -->`
on a line before a block.

`ingest` creates drafts in `Knowledge/_inbox/`, with its separate ledger at
`Knowledge/_state/ledger.sqlite` and unresolved questions in
`Knowledge/QUESTIONS.md`. `review` approves each draft as unverified or
explicitly confirms it with a reviewer name. `report` writes BA gaps and stale
links under `Knowledge/Reports/`.

## Ticket-driven API and parameter contract changes

Ticket notes can create proposals against captured API/parameter records. A
proposal is saved under `Knowledge/Changes/` and in the separate
`Knowledge/Changes/_state/changes.sqlite` ledger. Analysis reads captured API
notes and BRD entry notes; it does not call or replay the live application.
Captured requests and responses remain evidence of observed behavior. An
approved expected contract is versioned separately and shown in an
**Approved expected contract** section in the relevant API/parameter note.

```powershell
python -m capture_app.knowledge.cli propose-change --vault .\ObsidianVault --note-file .\ticket.md --ticket-id ABC-123 --provider local --local-model llama3.1
python -m capture_app.knowledge.cli list-changes --vault .\ObsidianVault
python -m capture_app.knowledge.cli show-change --vault .\ObsidianVault CHG-<id>
python -m capture_app.knowledge.cli edit-change --vault .\ObsidianVault CHG-<id> --changes-file .\approved-patch.json --reviewer "Reviewer Name" --rationale "Corrected target and requiredness" --resolve-question "Exact question copied from the proposal"
python -m capture_app.knowledge.cli review-change --vault .\ObsidianVault CHG-<id> --decision approve --reviewer "Reviewer Name" --rationale "Verified against PO decision" --po-approved-by "PO Name"
python -m capture_app.knowledge.cli apply-change --vault .\ObsidianVault CHG-<id> --reviewer "Reviewer Name" --rationale "Approved contract update"
```

`--note` accepts pasted text instead of `--note-file`. Use
`--provider openrouter` to send the note to the configured OpenRouter provider.
Ticket text and note contents are untrusted evidence. Proposed targets and
fields are validated against the supplied catalog; unknown or ambiguous
targets remain in `needs_info`. PO entries marked `unverified` cannot support
automatic approval: the authorized PO must be named with `--po-approved-by`.
Conflicts also require that explicit PO decision. Only an approved proposal can
be applied. Changes are rejected if the approved contract version changed after
proposal generation. The ledger retains each applied version and reviewer
rationale; captured API/parameter examples are never rewritten as expectations.
The reviewer patch file is a JSON array. Each item uses an existing catalog
`entity_id`, an allowed field, and the proposed value; evidence quotes must
appear verbatim in the ticket note:

```json
[
  {
    "entity_id": "Params/demowebshop_tricentis_com_checkout__StateProvinceId",
    "field": "requiredness",
    "proposed": "optional",
    "evidence_quotes": ["State is optional for countries that do not use states."],
    "requirement_ids": ["BR-04.13"],
    "rationale": "Matches the PO-approved checkout behavior."
  }
]
```

Use `--resolve-question` with the exact question text to record which open
questions the edit resolves. The reviewer can select any current API/parameter
catalog ID, including when initial matching found no candidates.

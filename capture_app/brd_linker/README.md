# BRD linker

The first stage inspects the vault without changing any Markdown notes or
calling a model. From the repository root, run:

```powershell
.\.venv\Scripts\python.exe -m capture_app.brd_linker.cli inspect
```

The command reads `config.yaml`, prints note/folder/frontmatter/tag/color-group
summaries and the proposed segment/API/Param classification, then writes exact
per-note wikilink counts to `out/inspect_notes.csv`.

The proposed classification is:

- Segments: `BRDs/raw/`
- APIs: frontmatter tag `api`
- Params: frontmatter tag `param`

Set `classification_confirmed: true` in `config.yaml` only after reviewing the
inspection output. Model-backed stages will require that confirmation. Keep
`OPENROUTER_API_KEY` in the environment; never put it in the vault or config.

## Safe sync

Sync reads Markdown BRDs from `sync.brd_dir`, the newest native replay JSON
from `sync.evidence_dir`, and request context from `sync.capture_db`. The
`sync adapters` command prints both adapter schemas and samples without
changing the vault:

```powershell
.\.venv\Scripts\python.exe -m capture_app.brd_linker.cli sync adapters
```

Generate and review a deterministic plan before applying it:

```powershell
.\.venv\Scripts\python.exe -m capture_app.brd_linker.cli sync plan
Get-Content .\out\sync_plan.md
.\.venv\Scripts\python.exe -m capture_app.brd_linker.cli sync apply --plan .\out\sync_plan.json
```

Apply verifies the plan's BRD, evidence, config, and vault fingerprints. It
backs up touched files under `.linker_backup/`, writes atomically, and records
an operation journal under `.capture_index/`. Resume or roll back an
interrupted apply with `sync resume <journal>` or `sync rollback <journal>`.
Soft-deleted notes are archived under `_archive/`; exclude that path from
Obsidian graph filters. `sync status`, `sync restore <source_id>`, and
`sync purge --older-than N` inspect or manage sync state.

Route normalization replaces numeric and UUID path segments with `{id}` by
default. Per-segment regex rules can be added under `key_normalization.patterns`.
Embedding and judge models are optional; when configured, only changed content
is embedded or judged. Without a judge model, candidates remain `proposed` and
do not become generated links automatically.

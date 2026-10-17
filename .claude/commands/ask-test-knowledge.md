
---
description: Ask the QC project knowledge base
argument-hint: <your question>
allowed-tools: Bash(./.venv/Scripts/python.exe -m capture_app.chat.cli search:*)
---
Run from the repository root, using the user's command arguments as the query:

./.venv/Scripts/python.exe -m capture_app.chat.cli search --vault ./ObsidianVault "$ARGUMENTS"

Answer using only the retrieved records. Cite their `id` and `source_path` values.

- Include relevant warnings and customer-experience notes.
- If the notes don't cover it, say so and list questions to ask the BA/PO.
- Do not modify any vault files.

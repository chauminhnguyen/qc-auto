---
description: Retrieve evidence to answer questions about the QC testing site and its behavior
agent: agent
---
Use the user's question as the query. Run this command in the terminal from the repository root:

./.venv/Scripts/python.exe -m capture_app.chat.cli search --vault ./ObsidianVault "${input:your question}"

Answer using only the retrieved records. Cite their `id` and `source_path` values.

- Include relevant warnings and customer-experience notes.
- If the records don't cover it, say so and list questions to ask the BA/PO.
- Do not modify any vault files.

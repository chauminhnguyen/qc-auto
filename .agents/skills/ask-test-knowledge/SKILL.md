
---
name: ask-test-knowledge
description: Retrieve evidence to answer questions about the QC testing site, its flows, APIs, parameters, warnings, and customer experience. Use for /ask-test-knowledge.
---
From the repository root, run the following command with the user's question as the final argument:

./.venv/Scripts/python.exe -m capture_app.chat.cli search --vault ./ObsidianVault "<the user's question>"

Use only the retrieved records to answer. Cite their `id` and `source_path` values.

- Include relevant warnings and customer-experience notes.
- If the records do not cover the question, say so and list specific questions for the BA/PO.
- Do not modify any vault files.

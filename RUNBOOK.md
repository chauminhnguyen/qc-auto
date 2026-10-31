# Running the toolchain from scratch

Run commands from the repository root. The supported command-line launchers remain at the root for compatibility; shared implementation modules are organized under `capture_app/` by workflow, state, reporting, storage, chat, and other feature areas. One-off maintenance scripts live under `tools/`. Generated data and reports continue to use their existing paths.

## 0. One-time setup

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

- `httpx` / `python-dotenv` — used by `enrich.py` / `openrouter.py`.
- `playwright` + the chromium install — used by `capture.py` (visible browser) and `ui_crawler.py` (headless browser).
- `param_report.py` needs nothing extra; it uses the Python standard library.

If you want `enrich.py`'s LLM-generated variants, put a free key from https://openrouter.ai/keys in a `.env` file next to the scripts:

```
OPENROUTER_API_KEY=your-key-here
```

Everything else below works without it.

## Task recipes: choose the outcome

Run commands from the repository root. On Windows, use `\.venv\Scripts\python.exe` in place of `python` if `python` is not the project's interpreter.

### A. Capture app behavior, create Obsidian notes, then ask a question

```powershell
python capture.py
# Use the visible browser, complete the flow, then type q and Enter to save.
python vault.py
# Enter the Obsidian vault path (or press Enter for .\ObsidianVault).
# Select unwanted captured APIs by number/range, then type y to confirm removal.
python -m capture_app.chat.cli build-index --vault .\ObsidianVault
python -m capture_app.chat.cli search --vault .\ObsidianVault "add to cart" --limit 5
python -m capture_app.chat.cli ask --vault .\ObsidianVault --debug-graph "What happens when a customer adds a product to the cart?"
```

`capture.py` writes captured API/session data to `.capture_index\index.sqlite` and renders Markdown notes under `APIs`, `Params`, and `Flows`. Run `vault.py` after capture to review and remove unwanted API entries before indexing. It previews the selected APIs and asks for confirmation. Removing an API also removes its API note and screenshots; parameter references are updated, and a parameter is removed only when no remaining API uses it. Cancel with `q` or decline the confirmation to make no changes. `build-index` refreshes the separate `.chat_index\index.sqlite`; run it after cleanup/capture before asking about new data (unless relying on `ask`'s automatic refresh).

To use Anthropic directly for `chat.cli ask`, set these values in the project `.env`:

```dotenv
CHAT_PROVIDER=anthropic
ANTHROPIC_API_KEY=your-anthropic-console-key
ANTHROPIC_MODEL=claude-sonnet-5
```

Then run the same `ask` command. `CHAT_PROVIDER` selects the chat CLI provider; `LLM_PROVIDER` separately selects the provider for BRD linking and testcase generation. The chat CLI sends Anthropic requests to its Messages API.

### B. Build or refresh the Obsidian graph nodes

There is no separate "build nodes" command. Capture and crawler workflows create the Markdown notes that Obsidian treats as nodes. `VaultWriter` also maintains `.obsidian\graph.json` with colors for API and parameter tags.

```powershell
python capture.py
# Finish and save a browser session.
python ui_transitions.py --vault .\ObsidianVault crawl --session <SESSION_ID> --crawl-id <CRAWL_ID>
```

Open `ObsidianVault` in Obsidian and choose **Graph view**. If the vault is not already open, choose **Open folder as vault** and select `.\ObsidianVault`. The crawler step is optional; it adds discovered UI paths and APIs. `build-index` is for chat search and does not create Obsidian nodes.

### C. Add BRDs to the vault and ask about their requirements

Put `.md`, `.txt`, `.pdf`, or `.docx` files in `BRDs\`, then ingest and index them before asking:

```powershell
python -m capture_app.brd_ingest.cli ingest --root .\BRDs --vault .\ObsidianVault --db .\brd_inventory.sqlite
python -m capture_app.chat.cli build-index --vault .\ObsidianVault
python -m capture_app.chat.cli ask --vault .\ObsidianVault --debug-graph "What requirements apply to checkout?"
```

`ingest` runs triage, extraction, raw-note creation, distillation, deterministic and LLM-assisted entity linking, Obsidian hub/backlink generation, and reporting. Each source BRD gets a hub note linked to its section notes and distilled requirement entries; those notes link back to the hub. Exact IDs, clear parameter-field matches, and sufficiently specific matches to screened captured response text are linked locally through their recorded API relationships. The semantic pass asks the configured LLM to match requirement wording to existing API, parameter, and flow notes, and allows it to return no match. The BRD Master Index connects all source hubs. It writes inventory/checkpoint data to `brd_inventory.sqlite` and notes under `ObsidianVault\BRDs\`. Tier C sources are listed for manual review and are not automatically distilled.

Ingest runs semantic linking automatically; no extra flag is needed. The standalone `link` command also runs the semantic pass by default. The model may abstain; shared generic terms or unrelated products are not linked. It receives BRD requirement text, API-to-parameter relationships, concise parameter definitions, and screened examples from user-facing response fields. Raw request payloads, response bodies, and examples from other parameter types are not sent.

`ask` searches the chat index and expands the matched BRD chunks with weighted graph neighbors, such as adjacent sections and linked API/parameter notes. `--debug-graph` prints the included chunks' source, edge kind, weight, and score to stderr so you can inspect why each passage was added. Use `--no-expand` for only the direct search matches, or `--expand-budget 12000` to raise the default context-character budget. The BRD `ingest` command refreshes BRD notes and links; follow it with `build-index` to refresh chat's separate index, then run `ask`. `ask` normally refreshes automatically too; use `--no-refresh` only when you intentionally want the existing index.

BRD linking uses the shared `LLM_PROVIDER` setting (`openrouter` or `anthropic`) from the process environment or project `.env`; shell variables take precedence. OpenRouter uses `OPENROUTER_API_KEY` and `OPENROUTER_MODEL`; Anthropic uses `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`. BRD linking calls the provider's existing JSON chat adapter once per 10 requirements, with retries disabled, and validates every returned entity path against the local vault catalog before writing links. Testcase generation reads the captured session and persisted live-run transitions locally and makes no model calls.

On each `ingest`, BRDs previously recorded from the selected source folder that are now missing are removed from the active BRD database. Their generated notes and source-specific hub are moved outside the vault to `./_archive/<date>/brd-ingest/` (the `_archive` folder beside `ObsidianVault`) so they disappear from Obsidian but can be recovered. The selected source folder is authoritative even when it is empty; an empty existing folder removes and archives all BRDs previously indexed from it. Make sure `--root` points to the intended BRD folder before running ingest.

Ingest also checks generated note frontmatter for BRD source ownership, so it can archive leftover segment, entry, or hub notes from older runs even if their SQLite inventory rows are already gone.

Non-empty Markdown and text BRDs are split into section notes even when triage rates their structure as low confidence. Each section note has the `brd-segment` tag; the vault graph colors that tag purple. After ingest, reopen the Graph view if Obsidian has not refreshed the nodes.

Testcase generation and chat indexing are separate optional workflows; they are not required to refresh the BRD graph in Obsidian.

The testcase command reads active distilled requirements from that same database and adds one source-linked case per requirement/business rule. It links a case to a captured API when route/name/field terms match; otherwise the requirement still appears as a BRD-only testcase. `--debug` shows those matches without making model calls or writing a workbook. If the ledger is stored elsewhere, pass its path with `--brd-db`.

PDF extraction uses the optional PyMuPDF package; if it is unavailable, PDF sources are routed to manual review. Review the package's license requirements before installing it. DOCX extraction uses the native XML path.

### D. Add QC notes to Knowledge, review them, then ask

Place source notes in a folder such as `Notes-raw`, then run:

```powershell
python -m capture_app.knowledge.cli ingest --vault .\ObsidianVault --notes .\Notes-raw --provider local --local-model llama3.1
python -m capture_app.knowledge.cli review --vault .\ObsidianVault
python -m capture_app.knowledge.cli report --vault .\ObsidianVault
python -m capture_app.chat.cli build-index --vault .\ObsidianVault
python -m capture_app.chat.cli ask --vault .\ObsidianVault --debug-graph "What do the reviewed QC notes say about checkout?"
```

Set the local provider URL/model in the environment if they differ from your local OpenAI-compatible endpoint. Review drafts before relying on them. Rebuild the chat index after review to include the reviewed notes.

For ticket/error notes that need an API or parameter contract update, create and
review a separate proposal. First make sure the vault has been populated by
capture (`capture.py`) and BRD ingestion (recipe C); proposal generation reads
the captured API/parameter catalog from
`ObsidianVault\.capture_index\index.sqlite`, the corresponding API/Params
Markdown notes, and BRD requirement notes under `ObsidianVault\BRDs\entries\`.
It ranks up to 20 relevant API/parameter records and 20 requirements before
calling the model. If the capture index or generated entity notes are missing,
run capture and its save step first. If BRD requirements are missing, ingest the
current BRDs first so the proposal can check the expected behavior.

The local provider requires an OpenAI-compatible chat-completions URL and model.
Set them in `.env` (loaded by the CLI) or pass both flags on the command. For
example, edit `.env` to include:

```text
KNOWLEDGE_LOCAL_URL=http://127.0.0.1:1234/v1/chat/completions
KNOWLEDGE_LOCAL_MODEL=your-model
# Optional, only if the local endpoint requires authentication:
KNOWLEDGE_LOCAL_API_KEY=your-local-key
```

Then create and review a proposal:

```powershell
python -m capture_app.knowledge.cli propose-change --vault .\ObsidianVault --note-file .\ticket.md --ticket-id ABC-123 --provider local --local-model llama3.1
python -m capture_app.knowledge.cli list-changes --vault .\ObsidianVault
python -m capture_app.knowledge.cli show-change --vault .\ObsidianVault CHG-actual-id-from-list
python -m capture_app.knowledge.cli edit-change --vault .\ObsidianVault CHG-actual-id-from-list --changes-file .\approved-patch.json --reviewer "Reviewer Name" --rationale "Corrected proposed field" --resolve-question "Exact question copied from the proposal"
python -m capture_app.knowledge.cli review-change --vault .\ObsidianVault CHG-actual-id-from-list --decision approve --reviewer "Reviewer Name" --rationale "Verified with PO" --po-approved-by "PO Name"
python -m capture_app.knowledge.cli apply-change --vault .\ObsidianVault CHG-actual-id-from-list --reviewer "Reviewer Name" --rationale "Approved contract update"
```

`propose-change` accepts exactly one of `--note-file` (UTF-8 Markdown/text)
or `--note "..."`; `--ticket-id` is optional. The command prints the proposal
JSON, including its `proposal_id`; `list-changes` also shows IDs and statuses.
Replace `CHG-actual-id-from-list` with that exact ID in every later command.
To use OpenRouter instead, select `--provider openrouter` and configure
`OPENROUTER_API_KEY` in `.env`; ticket text and selected evidence are sent to
that remote provider. Local is the default documented choice because it keeps
that model request on the configured local endpoint.

The stages are: ticket note → read-only catalog/BRD retrieval → LLM proposal →
Python validation and durable draft → reviewer edits/decision → apply approved
contract version and render it into the entity notes. The LLM cannot write to
the capture index or notes. The apply command writes the separate
`Knowledge/Changes/_state/changes.sqlite` ledger, proposal page under
`Knowledge/Changes/`, and the owned approved-contract section in affected API
or parameter notes. A change may be `needs_info` (unresolved question/target),
`po_conflict`, or `ready_for_review`; only an approved proposal is applicable.
Use `list-changes --status needs_info` or `--status po_conflict` to filter the
queue. Resolve questions with `edit-change` using exact question text and a
reviewer patch JSON array, then review the revised proposal. Do not run
`apply-change` until the before/after diff and PO trace have been checked.

The LLM proposes a versioned expected contract only. Captured request/response
samples remain unchanged and visible as observations. BRD entries marked
`unverified`, conflicts, and unresolved targets require a human PO decision;
name the authorized PO with `--po-approved-by` when approving those changes.
Applying a proposal whose base contract version is stale is rejected and
refreshes its diff for review. This workflow does not change or call a running
application and does not automatically update testcase workbooks; testcase
impact is proposal information that still needs a separate reviewed test
update.

### D1. Prepare and file a website UI bug ticket

Use this workflow for visible website defects such as apparel pagination or
newsletter button styling. These are Jira-ready Markdown drafts; this
repository has no Jira integration, so filing the issue itself is a manual step
in Jira. Do not use `propose-change` / `review-change` for a UI-only defect:
those commands propose API and parameter contract changes, and approval is
correctly blocked when the note has unanswered questions or no validated
contract changes.

Inspect and edit the matching draft from the repository root:

```powershell
Get-ChildItem .\jira_tickets\*.md
Get-Content .\jira_tickets\bug_apparel_catalog_pagination.md
notepad .\jira_tickets\bug_apparel_catalog_pagination.md
```

For the apparel pagination draft, record the actual page count, whether page 3
has products, product counts, active filters, sort order, configured page size,
and the relevant page URLs. Confirm with the PO that exactly two pages is the
approved expectation for the tested catalog data. For the newsletter styling
draft, record the current background/text colors and whether the mismatch
occurs in default, hover, focus, or disabled states. Capture screenshots and
browser/environment details, using safe test data and redacting sensitive
values.

Review the completed draft, then paste its contents into a new Jira Bug and
attach the sanitized evidence. For example, to review the apparel ticket after
editing:

```powershell
Get-Content .\jira_tickets\bug_apparel_catalog_pagination.md
```

Keep the Jira key and link in the ticket file after Jira creates it. If
triage reveals that the issue is actually an expected API/parameter contract
change, follow the ticket-driven proposal workflow above with a separate note
that states the observed behavior, expected behavior, and linked approved PO
requirement.

### E. Crawl a saved session, find a parameter path, then ask

```powershell
python ui_transitions.py --vault .\ObsidianVault crawl --session <SESSION_ID> --crawl-id <CRAWL_ID>
python ui_transitions.py --vault .\ObsidianVault find id --role request --resource-key "<DOMAIN:RESOURCE>"
python -m capture_app.chat.cli build-index --vault .\ObsidianVault
python -m capture_app.chat.cli ask --vault .\ObsidianVault --debug-graph "How can I set the id field?"
```

The crawl stores transitions and discovered API data in the capture database. Chat-index refresh reads the saved transitions. Use `--allow-submit` or `--allow-delete` only when you intend to permit those actions in a controlled test environment.

## BRD triage and inventorying

Use `scan` when you only want to profile files. For the full BRD-to-answer workflow, use task recipe C above.

```powershell
python -m capture_app.brd_ingest.cli scan --root .\BRDs --db .\brd_inventory.sqlite --json
```

The inventory stores each source path, content hash, tier, score, reasons, and review status. It is separate from `.capture_index` and `.chat_index`. Any supported source that yields readable section content gets notes regardless of its triage tier; empty or unreadable sources are listed for manual review.

## Native captured-session replay

The capture app includes its own Playwright runner. It reads steps and API events directly from the vault; it does not run Maestro or consume Maestro YAML. Install Playwright and Chromium as described in step 0, then run:

```bash
python -m capture_app.workflows.native_runner --vault ./testVault --session <SESSION_ID>
```

Native replay reports identify the captured sequence, action, target selector, and page for failures. An unlabeled tap can use its captured selector only when that selector uniquely identifies one element; otherwise the report says how many elements matched and the runner will not guess. Capture records clicks on their nearest semantic link/button/control (including clicks on images inside those controls) and uses the image alt text when available. API mismatches distinguish a request that was never sent, one that failed at the network layer, and one that was sent without a response during the replay wait. Each API note includes a standard cURL command under “cURL (Postman)”; copy it into Postman through **Import → Raw text**. Notes are regenerated whenever `capture.py` records that API. Credentials are redacted and must be replaced in Postman with safe test values when needed. Review the request before sending because it may change server-side data. Preflight failures also include up to five preceding captured steps, labeled as context rather than replay-passed steps. If the screenshot diagnostic replay stops earlier (for example, because a target was not found or the start page times out), the report identifies that separate stop. A start-page timeout is not a captured step. The report may embed a screenshot of the page state at timeout rather than the failed target.

If a capture contains redacted inputs, provide runtime values in a JSON file whose keys match the captured fixture names, then pass `--fixtures`:

```bash
python -m capture_app.workflows.native_runner --vault ./testVault --session <SESSION_ID> --fixtures ./test-fixtures.json
```

Do not commit real credentials or personal data in fixture files. Submit-like and delete-like actions stop replay unless explicitly enabled with `--allow-submit` or `--allow-delete`; API-linked `POST`, `PUT`, and `PATCH` actions are also held unless `--allow-submit` is provided. Use these only against a controlled test environment. Redacted values in URLs currently cannot be bound to fixtures and stop preflight. The runner fails on ambiguous targets rather than choosing one.

Each run writes Markdown and JSON artifacts under `<vault>/Reports/native_runs/`; a screenshot is captured at the first runtime failure when possible. Captured API calls are compared by method, route, and status, not full response bodies. Generated explanations are diagnostic hypotheses, not confirmed root causes. Unsupported steps, unavailable fixtures, and held actions are reported explicitly.

For a human-reviewed run, add `--interactive-review`:

```bash
python -m capture_app.workflows.native_runner --vault ./testVault --session <SESSION_ID> --fixtures ./test-fixtures.json --interactive-review
```

When a runtime failure has a saved screenshot, the runner pauses with the failed step, current URL, failure details, and screenshot path. Inspect the screenshot, then type `CONTINUE` to skip only that failed step and resume at the next captured step in the same browser session; press Enter or send EOF to stop. If a screenshot could not be saved, replay stops rather than inviting a screenshot-based override. Existing safety holds are never overridable through this prompt: `--allow-submit` and `--allow-delete` remain required where applicable. Reports mark approved skips as “completed with human override,” not as clean passes, and the command returns a nonzero exit status. Use the mode only when a person is available to inspect the evidence: prior actions may already have changed server state, and continuing may perform additional actions or submit data.

## Android device testing with Maestro

Install Android SDK Platform Tools and make sure `adb` is on `PATH`. Unlock the phone, enable USB debugging, and verify that `adb devices -l` lists it with state `device`. Android capture commands require an explicit serial with `--device-id`. Direct ADB calls go through the capture allowlist; capture does not install an APK or helper app.

The starter flow in `android/flows/smoke.yaml` launches the configured app and checks for visible text. Supply a value that is stable on the app's initial screen:

```powershell
python -m capture_app.workflows.maestro_device_runner `
  --flows .\android\flows\smoke.yaml `
  --app-id com.example.app `
  --device-id "YOUR_DEVICE_SERIAL" `
  --expected-text "Home"
```

Provide the selected serial from `adb devices -l` with `--device-id <SERIAL>`. A flow directory may be passed to run a suite of `.yaml` and `.yml` flows. Each run checks the app installation before starting Maestro and writes `summary.json`, `commands.log`, a JUnit report, and Maestro test artifacts under `out/android_runs/`. A preflight error or failed Maestro run returns a nonzero exit code. Profiles control whether Maestro is permitted; it is disabled for the restricted-app profile template.

For custom flows, use `${APP_ID}` as the flow's `appId` and begin with `launchApp`; the runner supplies `APP_ID` automatically. The starter flow also reads `${EXPECTED_TEXT}`, supplied by `--expected-text`.

### Android capture runs (Maestro + ADB + mitmproxy)

Android profiles live in `android/apps.yaml`. The `speedtest` profile targets Ookla Speedtest. Add another profile with its package ID, flow path, network source order, host allowlist, `ui_snapshots`, and `maestro_permitted` settings when switching apps. Use test accounts. The tool warns when the vault path appears to be inside Git or a common synced folder. An empty host allowlist is discovery mode; the run summary lists observed hosts, and proxy connection/TLS failures by host when no events arrive.

Manual capture is for operating the app directly on the phone while recording touch interactions and network evidence. It never taps for you. It does not save screenshots, recordings, raw proxy archives, or raw logcat. The Speedtest profile enables text hierarchy snapshots; the restricted profile template disables snapshots and Maestro. With snapshots disabled, touch steps contain only the `secure_input` placeholder (no label or coordinates). Editable/password fields, keyboard windows, and digit-keypad-like screens are also treated as secure input. Foreground package/activity is checked from `dumpsys window`; steps outside the profile app are discarded. Hierarchy text is sanitized before writing. Confirm with your company that text hierarchy dumps are allowed before enabling them.

The manual run summary includes proxy diagnostics without request contents: total, phone-matched, and other client connections; HTTP CONNECTs; HTTP request/response counts; TLS handshake successes/failures; upstream connection failures; and per-host counts. Zero total proxy clients usually means the phone did not route traffic to the laptop proxy. Other-client connections with zero phone-matched connections can indicate the phone's IP differs from its ADB serial. CONNECTs with TLS client failures means the app reached the proxy but did not complete TLS with it. These counts help distinguish proxy setup from app traffic that is pinned or uses a non-HTTP protocol.

Start a manual session with the selected device serial:

```powershell
python -m capture_app.workflows.android_capture manual `
  --app speedtest `
  --device-id "YOUR_DEVICE_SERIAL"
```

Use the app on the phone, then type `q` and Enter in the terminal to stop. The session is checkpointed periodically and partial evidence is retained if ADB disconnects or the process is interrupted. Touch events require Android to grant `getevent` access; if access is denied, the summary reports the failure. Touch timestamps are calibrated from `/proc/uptime`; calibration is refreshed during long sessions. Each hierarchy attempt runs about 800 ms after touch-up, and timed out or rate-limited snapshots are listed as missing UI evidence. Check the phone during a real-device pilot for app warnings, logout, or other reactions when hierarchy dumps run.

The network source order is selected in each profile: `proxy`, `app_log`, and `logcat`. A source that starts and returns no events is still a valid run; interaction evidence continues to be recorded. Startup/read/parse failures allow the runner to try the next configured source. For an in-app export, choose the app's normalized JSON export option, save it in the phone's Downloads folder, copy the file to the laptop yourself, and pass that local path with `--network-export <PATH>`. The JSON format is a list of events or an object with an `events` list; each event has `method`, `url`, and optional `status`, headers, bodies, and `ts`. HAR 1.2 and normalized JSON can also be imported with `import-network`.

Install the optional laptop-side capture dependencies into the project environment if using the proxy source:

```powershell
python -m pip install -r requirements-android.txt
```

For the scripted Speedtest results run, clear the proxy in the phone's current Wi-Fi settings, then run:

```powershell
python -m capture_app.workflows.android_capture results `
  --app speedtest `
  --device-id "YOUR_DEVICE_SERIAL"
```

For a separate scripted API run, connect the phone and laptop to the same trusted network. On first setup, start a temporary proxy to create and serve the mitmproxy CA:

```powershell
mitmdump --listen-host 0.0.0.0 --listen-port 8080
```

Set the phone's Wi-Fi proxy to the laptop's LAN IPv4 address (find it with `ipconfig`) and port `8080`. In the phone browser, open `http://mitm.it`, download the Android CA, and install it through Android's certificate settings. Stop the temporary proxy with Ctrl+C. The capture command starts its own proxy on the same port. Do not use the API run for Speedtest measurements.

Then run:

```powershell
python -m capture_app.workflows.android_capture api `
  --app speedtest `
  --device-id "YOUR_DEVICE_SERIAL"
```

The scripted runner starts and stops `mitmdump` on the laptop. Manual API capture uses the same laptop proxy source when configured; set the phone's Wi-Fi proxy and trust its CA yourself. Capture never bypasses certificate pinning. Apps that ignore the proxy, reject the CA, or use non-HTTP transports may yield no API events. In that case interactions are still saved, and proxy host failure counts appear in the summary.

Every UI action in a scripted capture flow should be preceded by `waitForAnimationToEnd` and `runScript: ../scripts/marker.js` with a unique `STEP_ID` and label. Hierarchy dumps run after the main flow in separate Maestro prefix replays. Prefix replays require linear top-level flows and repeat earlier actions, so only enable `allow_prefix_replay: true` after confirming those actions are safe to repeat. Process-scoped logcat is redacted as it is written, and `clock_offset.json` records device/laptop time alignment. Maestro is permitted for the Speedtest profile; do not enable it for restricted profiles.

Run results are saved under `out/android_runs/<run-id>/`. API events and marker steps are ingested into the configured vault session, so `param_report.py` and the session graph builder can read them. To import an app export:

```powershell
python -m capture_app.workflows.android_capture import-network `
  --app speedtest --input .\network.har --format har
```

Normalized JSON is a list, or an object with an `events` list. Each event has `method`, `url`, `status`, `request_headers`, `payload`, `response_headers`, `response_body`, and `ts` (epoch seconds or ISO-8601); optional `response_ts`, `step_id`, and `correlation_status` fields preserve timing/correlation. Manual events are linked to the interaction window by time. Late or unassigned requests are marked ambiguous; steps with no calls are valid. Image and PDF responses are stored only as content type, byte size, and SHA-256 hash.

**Get `d3.min.js`.** `visualizer.py` looks for a file literally named `d3.min.js` next to itself and copies it alongside every HTML report it writes. Without it, the interactive graph won't render (you'll still get a static fallback view). Download one version and drop it in the same folder:

```bash
curl -o d3.min.js https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js
```

## 1. Capture a manual session — `capture.py`

```bash
python capture.py
```

- Prompts for a vault folder (Enter for the default `./ObsidianVault`) and a starting URL (Enter for `https://petstore.swagger.io/`).
- Opens a **visible** Chromium window. Click and type through the app yourself — every click/input and every API call it triggers gets recorded.
- In the terminal: press **Enter** to pause/resume capturing, type **`q`** + Enter to stop and save.
- Do at least one real end-to-end flow here (e.g. create a pet, view it, update it) — this manual session is the "standard flow" everything else compares against, and it seeds the crawler's starting point.

You now have `./ObsidianVault/` — a SQLite index plus Markdown notes (`APIs/`, `Params/`, session notes) you can open directly in Obsidian.
Capture also writes `Reports/flow_visualizations/<SESSION_ID>/happy_path.html`, a detailed block diagram of the saved UI steps and their directly correlated API calls.

To regenerate that diagram for a saved session without opening a browser, run `python capture.py init`. It uses the latest session in `./ObsidianVault` by default; pass `--vault` and/or `--session` to select another one:

```powershell
python capture.py init --vault .\ObsidianVault --session <SESSION_ID>
```

## 2. (Optional) LLM-generated test variants — `enrich.py`

```bash
python enrich.py
```

Interactive prompts walk you through picking a captured API and generating payload variants via OpenRouter. Needs the `.env` key from step 0. Skip this entirely if you just want the state-machine/crawler pipelines below.

## 3. Offline replay — `state_machine_system.py`

Replays the captured session against `GlobalStateStore`/`FailStateStack` using each API's historically-captured status (no live calls):

```bash
python state_machine_system.py --vault ./ObsidianVault --session <SESSION_ID>
```

Leave off `--session` to use the most recent one. Produces:

- `State_Execution_Report.md` — good/bad case tables, fail-pool tree
- `state_graph_visualization.html` — the interactive graph

**Known caveat from the earlier review:** this path's composite conditions come from `state_executor.py`'s crude keyword heuristic (any state named buy/checkout/purchase/order gets hardcoded conditions nothing ever satisfies), and `topological_sort()`'s ordering bug means most non-entry states will likely show up as "skipped" regardless of whether they'd genuinely succeed. Don't take a big "failed" count here at face value yet — say the word if you want those two fixed.

## 4. Live crawl + compare — `live_runner.py`

Actually drives a headless browser and compares what it finds against the standard flow:

```bash
python live_runner.py --vault ./ObsidianVault --session <SESSION_ID> --verbose
```

- Add `--allow-submit` to let it actually click submit/buy/checkout-like buttons (held back by default).
- Add `--allow-delete` to additionally allow delete/remove actions (separate, stricter opt-in).
- Produces `State_Execution_Report.md`, `state_graph_visualization.html` (from `report.py` — see caveat below), and `live_graph_visualization.html`.
- Updates `Reports/flow_visualizations/<SESSION_ID>/full_flow.html` with observed exploration branches beside matching happy-path steps. The renderer matches by source page, action, API evidence, and captured route; when those signals are insufficient, it attaches the branch to the best-fit happy-path step and labels that connection as inferred. A separate section is used only when the capture has no happy-path steps to anchor to.

**Known caveat:** `state_graph_visualization.html` from this path has a node/coloring mismatch (UI-fingerprint graph vs. state-level case names) — `live_graph_visualization.html` is the one with correct coloring for a live run.

## 5. Persist crawl paths + "how do I reach this param" — `ui_transitions.py`

Run a crawl and save it (not just print it — this is what makes it queryable later):

```bash
python ui_transitions.py --vault ./ObsidianVault crawl --session <SESSION_ID> --crawl-id run1
```

Add `--allow-submit` / `--allow-delete` the same way as step 4 if you want those actions explored too.

Then look up how to reach a param from the frontend:

```bash
python ui_transitions.py --vault ./ObsidianVault find id --role request
python ui_transitions.py --vault ./ObsidianVault find id --role response --resource-key "petstore.swagger.io:pet"
python ui_transitions.py --vault ./testVault crawl --session 20260917T140421 --allow-submit --crawl-id run9

python live_runner.py --vault ./ObsidianVault --session <SESSION_ID>
python testcase_gen.py --vault ./ObsidianVault --session <SESSION_ID> --out testcases.xlsx
python testcase_gen.py --vault ./ObsidianVault --session <SESSION_ID> --run-id <LIVE_RUN_ID> --out testcases.xlsx
```

`live_runner.py` persists its ordered UI transitions in the capture index, linked to the capture session and live-run id. `testcase_gen.py` reads the captured steps and API observations plus one live-run transition set. By default it uses the latest live-run data for the session and makes no model calls. The workbook contains one full happy-path case and one case per observed transition, including transitions whose actions were held or failed.

Field profiles are opt-in. Run `python testcase_gen.py --vault ./ObsidianVault --session <SESSION_ID> --run-id <LIVE_RUN_ID> --probe-profiles` to replay each matched API baseline and send up to 10 one-field probes per recorded input. Mutating endpoints require confirmation. If captured credentials were redacted, the command prompts for replacements; they stay in memory and are not included in probe evidence or model prompts. The raw, redacted probe log is resumable in the capture index. Use `--debug-profiles` to inspect stored outcome groups and their supporting probe rows without sending requests, and `--profile-driven` to generate additional cases from an existing run's profiles. Probe and profile-driven modes append a Technical Evidence column; running with no new flags preserves the existing workbook layout and behavior. Profiles only cover inputs that can be conservatively linked to request parameters.

To add written BRD/Knowledge rules from the chat index, combine `--graph-rules` with probing or profile-driven generation. Example: `python testcase_gen.py --vault ./ObsidianVault --session <SESSION_ID> --run-id <LIVE_RUN_ID> --probe-profiles --graph-rules --out testcases.xlsx`. Graph weights rank candidates only; field-name/label relevance and cited source node IDs are required before a rule is used. The enhanced workbook adds `Basis`, and contradictions appear on a `Rule Review` sheet. Contradicted rules do not generate rule-derived cases. `--debug-field "Recipient's Email" --graph-rules` prints graph candidates, stored probes, and constraint states without an LLM call or HTTP replay.

- `--role request` = "how do I **set/change** this param" (a form that submits it).
- `--role response` = "how do I **see** this param" (an action whose response contains it).
- `--resource-key` disambiguates when two unrelated things share a field name (e.g. a pet's `id` vs. an order's `id`) — leave it off for a broader, unscoped match.

Each result prints numbered click-by-click directions ending in the actual action that sets/reveals it. Run `crawl` a few times (different sessions, with/without `--allow-submit`) to build up a richer set of paths before you rely heavily on `find` or the reports in step 6.

## 6. Turn a param into a shareable report — `param_report.py`

Takes everything the vault knows about **one parameter** and writes a single Markdown report: plain-language steps for how to **see** it and how to **change** it through the real site, plus ready-to-paste `curl` commands that call the same API directly. It reads what steps 1 and 5 stored, so run those first.

```bash
python param_report.py --vault ./ObsidianVault id
python param_report.py --vault ./ObsidianVault id --resource-key "petstore.swagger.io:pet"
python param_report.py --vault ./ObsidianVault id --resource-key "petstore.swagger.io:pet" --role request -o pet_id_report.md
python param_report.py --vault ./testVault updatetopwishlistsectionhtml -o report.md
```

- The parameter name goes on its own, with no `name=` in front — e.g. `success` or `id`, exactly as you'd give it to `find`. (`param_name=success` will not work.)
- `--resource-key` — same meaning as in step 5. **Required** when the name exists under more than one resource key (the script lists them and stops).
- `--role request|response|both` — which side to report on. Default is `both`.
- `-o` / `--output` — where to write the file. Default is `param_report_<param>[__<resource-key>].md` in the current folder.

When it finishes it prints one line, e.g. `wrote param_report_id.md: 2 place(s), 2 distinct API call(s), 1 with a click path`.

**What's in the report** (sections are numbered automatically; ones with nothing to say are skipped):

1. Short version — what the field is, and how many places/calls it shows up in
2. Where it shows up — one line per API call, marked reachable / not yet mapped
3. Step by step: seeing it — click-by-click directions (response side)
4. Step by step: changing it — click-by-click directions (request side)
5. Calling it directly — one `curl` block per distinct API call, plus a "Getting a fresh cookie" mini-guide
6. Why this matters — **generic text**; edit it before you share the report
7. What we found / recommendation — counts, and what's still unmapped

**What to expect:**

- **Click paths come from the crawl.** A param that only ever showed up in a manual `capture.py` session has no click path yet. The report says so plainly ("we know this field exists in the API, but haven't yet mapped which button or page triggers it") and still gives you the `curl` block if the vault stored that call. Run the crawl in step 5 to fill these in. When several routes reach the same call, the shortest is written out in full and the others appear as one-line alternatives.
- **No secrets end up in the file.** `Cookie` / `Authorization` headers and secret-looking fields (password, token, csrf, session, …) are replaced with placeholders such as `<paste your own session cookie here ...>`. To fill one in, follow the "Getting a fresh cookie" section inside the report (browser dev tools → Network tab → copy the header from the request).
- **The `curl` commands are bash syntax.** Paste them into Postman via **Import → Raw text**, or run them in Git Bash / WSL / macOS / Linux. On Windows, PowerShell's `curl` is a different program and won't work.
- **POST/PUT/DELETE commands change data.** Only run them against a system you're allowed to test.

**If something looks off:**

- `error: No parameter named 'x' in the vault.` — it suggests close matches; otherwise check the spelling, or capture/crawl the flow that uses it first.
- `error: 'x' exists under N different resource keys` — pick one from the list it prints and add `--resource-key`.
- A `curl` block is marked as a "skeleton", or a call says it has "no stored URL" — the script couldn't find that call's headers/body/URL in the vault's `apis` table. Run:

  ```bash
  python param_report.py --vault ./ObsidianVault --dump-schema
  ```

It prints the tables the report reads (cookies/tokens/passwords are scrubbed, but skim it before pasting it anywhere) — that output is what's needed to fix the lookup.

**Optional Word version:** if you have `pandoc` installed, `pandoc param_report_id.md -o param_report_id.docx` converts a generated report. It isn't built into the script.

## Vault chat search and Q&A — `capture_app.chat`

The chat package builds a separate SQLite search index at `<vault>/.chat_index/index.sqlite`. It reads Markdown notes from the vault; it does not write to the capture index at `.capture_index/index.sqlite`. Run these commands from the repository root with the project Python environment. The chat package uses only the Python standard library and SQLite; no extra package installation is required.

Build or refresh the index after capturing data, ingesting BRDs, reviewing Knowledge notes, or editing vault Markdown:

```powershell
python -m capture_app.chat.cli build-index --vault .\ObsidianVault
```

The command rebuilds the chat index from the vault notes and prints the database path and indexed note count. Use a different vault with `--vault`; `--db` optionally selects a different chat-index file. It also reads persisted `ui_transitions` rows from `.capture_index/index.sqlite` through a read-only SQLite connection and indexes their action paths, outcomes, and triggered API calls. It never modifies that capture database. BRD raw and distilled Markdown notes are included when they are under the vault; run the BRD `ingest` command first, then rebuild this index.

`live_runner.py` writes its in-memory transition paths, API discoveries, summary counts, and diagnostic report into the same chat index before creating the HTML graph. Those records are searchable immediately; no separate index rebuild is needed after a live-runner run. A later `build-index` refresh preserves those live-runner records while refreshing the vault notes and persisted `ui_transitions` data.

Search without calling an LLM:

```powershell
python -m capture_app.chat.cli search --vault .\ObsidianVault "subscribe button" --limit 5
```

Ask a question using the top search results as context:

```powershell
python -m capture_app.chat.cli ask --vault .\ObsidianVault --debug-graph "What color should the subscribe button be?"
```

The default provider is a local OpenAI-compatible chat-completions endpoint at `http://127.0.0.1:11434/v1/chat/completions`, suitable for Ollama when its compatible API is available. Start Ollama and make sure the configured model is installed before using `ask`. Override the provider settings with environment variables in PowerShell:

```powershell
$env:CHAT_PROVIDER = "local"
$env:CHAT_API_BASE = "http://127.0.0.1:11434/v1/chat/completions"
$env:CHAT_MODEL = "llama3.1"
python -m capture_app.chat.cli ask --vault .\ObsidianVault --debug-graph "Explain the add-to-cart flow"
```

For OpenRouter, set the provider, model, and API key (the existing `OPENROUTER_API_KEY` variable is also recognized):

```powershell
$env:CHAT_PROVIDER = "openrouter"
$env:CHAT_API_BASE = "https://openrouter.ai/api/v1/chat/completions"
$env:CHAT_MODEL = "<openrouter-model-id>"
$env:OPENROUTER_API_KEY = "<your-key>"
python -m capture_app.chat.cli ask --vault .\ObsidianVault --debug-graph "What is known about checkout?"
```

`ask` also accepts per-command `--provider`, `--api-base`, `--model`, and `--api-key` options. Avoid putting API keys directly in command history; prefer environment variables. `CHAT_TEMPERATURE` and `CHAT_MAX_TOKENS` configure generation. `CHAT_NUM_CTX` is currently read by the config but is not passed through to the provider request, so configure Ollama's context size separately.

**Current implementation scope:** `search` and `ask` are an initial retrieval prototype. The index currently creates one chunk per Markdown note (not heading-based chunks), and `build-index` rebuilds it rather than incrementally updating it. `ask` currently searches for matching notes and passes them to the provider; it does not yet implement the planned graph warning expansion, router/list queries, schema-validated claims, query logging/feedback, or golden-set evaluation. Verify the cited source notes before treating an answer as authoritative.

## Suggested order for a first run

Start with task recipe A to capture a session and ask a question. Use recipe B to inspect generated notes as Obsidian graph nodes, recipe C for BRD requirements, recipe D for QC/Knowledge notes, or recipe E to add crawl paths and look up parameter journeys. The detailed sections below cover replay, crawling, and report options.


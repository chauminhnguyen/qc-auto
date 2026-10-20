# Exploration pipeline -- integration notes

Diff engine -> AI classify (cached, batched per screen) -> test synthesis
-> scoring bonus -> (your existing) execute/verify -> coverage report.
Wired against the application's shared UI, vault, and crawler modules.

The implementation lives in `capture_app/`; this pipeline is under
`capture_app/exploration/`. Run it from the repository root so the existing
vault paths and command-line defaults remain relative to the working directory.

## Files

- `models.py` -- re-exports `Action`/`Interactable`/`ScreenIndex` straight
  from `ax_tree.py`; adds the pipeline-only shapes it doesn't need itself
  (`ScreenSnapshot`, `TakenAction`, `ClassifiedAction`, `SyntheticTestCase`).
- `diff_engine.py` -- pure set-subtraction over `ax_tree.rank_actions()` /
  `dedupe_key()`, with anything `ax_tree.is_denied()` flags dropped too --
  if `ui_crawler.py`'s own crawl would never click it, it's not a candidate
  here either. No API calls, no external deps.
- `vault_adapter.py` -- turns a `vault.py` session's `steps` table rows
  into `TakenAction`s, using the same tap/input/select-kind branching
  `_render_step_lines()` uses. Takes a `VaultWriter` directly (reads via
  `vault.conn`), not a raw db path. Known gap: no `dup_index` column on
  `steps`, so taken actions default to `dup_index=0` -- see the docstring
  for why that's argued to be correct, not just a fallback.
- `live_snapshot.py` -- **new**. `LiveSnapshotter`, a context manager that
  opens one headless Playwright browser for the whole run and reuses
  `ui_crawler.py`'s own `HeadlessCrawler._snapshot()` (i.e. the real
  `SNAPSHOT_JS`) for every page the diff engine needs re-inventoried. It
  reaches into `HeadlessCrawler`'s underscore-prefixed helpers
  (`_snapshot`, `_goto_resilient`, `_settle`) on purpose, so this can never
  quietly drift out of sync with the snapshot logic -- promote those three
  to public methods if you'd rather not do that.
- `cache.py` -- `screen_classifications` table living in the *same* sqlite
  file as vault's own tables (`vault.conn`), not a second stray `.sqlite`.
- `ai_classifier.py` -- the only file that calls the Anthropic API. One
  batched call per uncached screen. Default model
  `claude-haiku-4-5-20251001` (env var `EXPLORATION_CLASSIFIER_MODEL` to
  override). Requires `pip install anthropic` + `ANTHROPIC_API_KEY`.
- `synthesis.py` -- turns a `worth_testing` classification into a step-list
  compatible with `export_maestro_flows()`'s shape. Delete-like and
  submit-like actions are emitted as a `"held"` step instead of a real
  click unless `allow_delete`/`allow_submit` is passed through -- the same
  gate `HeadlessCrawler.__init__` already applies, so this pipeline can't
  become a backdoor around it.
- `capture_app/exploration/scoring_patch.py` -- `coverage_gap_bonus()`, meant to be added as an
  extra additive term inside `best_first_crawler.py`'s `_score()`.
- `report_patch.py` -- `render_coverage_section()`, meant to be appended to
  whatever `report.py` already generates.
- `run_exploration.py` -- `run(vault, session_id, live_snapshot_fn, ...)`
  for full control, plus `run_with_live_crawler(vault, session_id, ...)`,
  which opens a `LiveSnapshotter` for you -- the one-call entry point for a
  normal run.

## Smoke test (already run twice, safe to rerun)

`smoke_test.py` opens a real `VaultWriter` in a temp directory (so it's
validated against your actual `steps` table schema, not a guess), inserts
one row (a tap on "Add to cart"), fakes a live snapshot of a product page
with three buttons, and asserts the diff engine reports "Add to wishlist"
and "Compare" as untested while excluding "Add to cart" -- using your real
`ax_tree.py` and `vault.py`.

## Open decision (from the pipeline diagram)

Once a sibling action is executed and verified, does it fold back into the
standard-flow baseline (so future diff runs stop flagging it), or stay in a
separate exploration-coverage ledger? `untested_actions()` just takes
whatever `TakenAction`s you pass it, so growing the baseline is a matter of
appending newly-verified actions to that list before the next run.

## Not yet run for real

`live_snapshot.py` reuses already-exercised `HeadlessCrawler` code paths,
but I haven't launched an actual browser against a live site to confirm
it end-to-end -- this environment doesn't have a reachable target site or
a guaranteed Playwright/Chromium install. Worth a real run on your side
before trusting it fully, particularly the `_goto_resilient` retry path on
a slower page.

## Quota behavior

Calls scale with distinct `ScreenIndex` values, not clicks, products, or
crawl revisits. A full site should land in the tens of calls, and a second
run against an unchanged site should cost close to zero.

## Write-safe UI exploration

The Playwright crawlers install a request gate before page navigation and
block service workers so application writes cannot bypass routing. Safe reads
pass through; state-changing requests default to an in-memory mock and are
queued in `decisions.json` for a bounded end-of-run review when a terminal is
available.
Mocked creates, patches, and deletes remain visible to later reads in that
browser run. Discovery runs once per target origin before the first crawl,
records a credential-scrubbed HAR, probes common OpenAPI paths, and seeds the
endpoint lists. Set `WRITE_SAFE_DISCOVERY=0` to skip that phase.

Authentication and action approval are independent. Without `--auth-state`,
the crawler opens a visible browser and waits for you to log in before it
explores, even when the site allows anonymous access. With `--auth-state`, it
restores the saved Playwright session and skips that pause. Add `--interactive`
to ask before each eligible crawler action; a declined action is skipped and
recorded. This action approval does not override `--allow-submit` or
`--allow-delete`. The separate endpoint review at the end records decisions
for later runs; unanswered endpoints remain mocked.

Examples:

```powershell
# Saved login; crawl headlessly
python ui_transitions.py crawl --vault ./ObsidianVault --auth-state ./auth-state.json

# Saved login plus approval before each action
python ui_transitions.py crawl --vault ./ObsidianVault --auth-state ./auth-state.json --interactive

# No saved login; open browser and wait for manual login
python ui_transitions.py crawl --vault ./ObsidianVault

# The live-runner entry point supports the same independent options
python live_runner.py --vault ./ObsidianVault --auth-state ./auth-state.json
python live_runner.py --vault ./ObsidianVault --auth-state ./auth-state.json --interactive
```

Configure `WRITE_SAFE_AUTH_PATHS` as a comma-separated list of auth path
prefixes (for example `/login,/csrf`, which permits POST) if the site must
submit login or session traffic during exploration. Prefix a path with a method
when needed (for example `PUT:/auth/refresh`). Those requests pass only when configured.
The endpoint decision and report locations can be changed with
`WRITE_SAFE_DECISIONS`, `WRITE_SAFE_REPORT_DIR`, and
`WRITE_SAFE_DISCOVERY_DIR`.

Real writes stay mocked unless the endpoint is approved, the request carries
the configured test-account identity signal, and it either creates a record
with a `QA_` tag or targets an ID returned by a real tool-created create.
Configure `WRITE_SAFE_ACCOUNT_HEADER` with its expected value, or configure
`WRITE_SAFE_ACCOUNT_COOKIE` with its expected value, only when the site exposes
a reliable signal for the test account. Without that signal, approved writes
still run against mocks. Set `WRITE_SAFE_QA_FIELD` only when the API supports a
string field the gate can mark with `QA_<run>_...` on approved creates. Review
choices apply on a later run; unanswered and
deferred items remain queued and mocked. Reports under `safety_reports/`
include requests, action outcomes, queued endpoints, list changes, skips, and
mock limitations. Checkpoints let a later run replay the last successful
action path; actions that fail repeatedly are skipped.

Mocked UI success does not verify server validation, permissions, computed
fields, server ordering or counts, WebSockets/SSE, triggered jobs, uploads, or
multi-step server state.

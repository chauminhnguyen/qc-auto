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

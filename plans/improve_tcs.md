## Plan: Trustworthy, Executable Test Cases

Fix the generator at its source, then regenerate `testcases.xlsx` as a human-runnable, HTTP-level regression suite. Expand beyond the single captured session, but keep behaviors not yet established as explicit exploration work until verified in a controlled environment with disposable accounts/data; never run mutating checkout, order, or voting cases against the shared public demo.

**Steps**

**Phase 1: Correct captured-flow logic**

1. Repair dependency construction in `D:\vsf\capture\standard_flow.py` using the ordered `session_api_events` sequence: retain valid predecessor/data dependencies, filter self-dependencies created by repeated refs, and make dependency order stable. Add tests for repeated refs and ensure no state requires itself.
2. Bind each generated captured example to one specific event observation in `D:\vsf\capture\testcase_gen.py`: request, response, status, action label, sequence number, and exported flow must come from the same occurrence. Do not combine API-table samples with a different event's status or choose an arbitrary first matching flow.
3. Make preconditions executable and semantically consistent: render the complete prerequisite chain for happy-path checkout; validate every generated missing-precondition reference against a real dependency; reject self-dependencies; represent the withheld prerequisite as absent in setup and do not present its flow as setup to execute. Drop unsupported 4xx/412 assumptions unless observed.
4. Correct applicability and deterministic assertions: separate COD from card-only cases based on the selected payment method; use a non-expired date for any verified card scenario; create an autocomplete positive case with a term that returns suggestions and a minimum-length boundary only after the configured app behavior is checked; classify the `/addproducttocart/details/.../2` operation by verified cart type/action and captured result, not path wording alone. Remove invented outcomes and either record one observed assertion or label the scenario `needs exploration`.

**Phase 2: Make workbook runnable and safe**
5. Change the generated suite to one test level: HTTP request/response. Include method, complete URL/path, required headers/authentication source, concrete body/query values, and one deterministic response/status assertion. Keep workbook status labels distinguishing captured/not re-tested, verified in controlled environment, proposed, and needs exploration. Separate required setup from deliberately missing setup.
6. Replace session-specific IDs and personal-looking email data with documented placeholders resolved from a dedicated test account/fixture. Do not place credentials or real personal data in the workbook. Add replay-risk metadata and explicit disposable-state requirements for order confirmation, poll voting, cart mutation, and other non-idempotent actions; generated output must not imply shared-demo replay is safe.
7. Stabilize and clean workbook presentation: deterministic endpoint/case ordering; complete flow/event references without truncated `(+N more)` or embedded formatting artifacts; preserve status codes in both sheets; retain observed-vs-proposed labels; ensure each row has a pass/fail-able assertion or `needs exploration`. Update `write_excel()` and the runbook accordingly.

**Phase 3: Expand coverage and verify**
8. Add a coverage matrix beyond the one recorded path, traceable to the feature/scenario and its verification status. Include empty-cart checkout; required address fields (empty city, country 0, state required where applicable); pickup versus shipping; guest versus registered checkout; quantity over stock/limit; missing required product attributes; cart update/remove; coupons; wrong HTTP method; and safe XSS/SQL-injection input handling for search/address. Include autocomplete minimum-term behavior and positive suggestions. Use a controlled test instance/account and disposable state for behavior checks; when that environment or fixture is unavailable, leave exact outcomes `needs exploration` rather than guessing.
9. Verify observable outcomes in that controlled environment before turning proposals into assertions. Record actual HTTP status and response shape, including whether checkout errors return HTTP 200 with an error payload. Do not automate or manually replay mutations against the shared public demo.
10. Regenerate `testcases.xlsx` only after generator tests and the controlled checks pass; inspect the resulting workbook and update `RUNBOOK.md` with environment/fixture prerequisites and safe execution boundaries.

**Relevant files**

- `D:\vsf\capture\standard_flow.py` — `build_standard_flow`, ordered event query, temporal/data dependency assembly.
- `D:\vsf\capture\testcase_gen.py` — `BATCH_SYSTEM_PROMPT`, `_real_status_for_ref`, `_find_flow_file`, `_happy_path_row`, `_endpoint_context`, `_case_to_row`, `generate_all`, `write_excel`, and CLI `main`.
- `D:\vsf\capture\vault.py` — event-level capture/export records and `get_params_for_api`, for consistent event provenance and flow sequence references.
- `D:\vsf\capture\test_testcase_gen.py` — extend existing batching/model-output tests with dependency, case-validation, evidence-binding, applicability, and workbook assertions.
- `D:\vsf\capture\RUNBOOK.md` — document controlled test environment, fixture/account configuration, and workbook replay safety.
- `D:\vsf\capture\testcases.xlsx` — regenerate as a derived artifact after source changes and validation; inspect both sheets.
- Captured references: `D:\vsf\capture\testVault\Flows\20260917T140421.md`, checkout flows 08/14, payment-method API/parameter notes, autocomplete API/parameter notes, and wishlist-triggered flow 02.

**Verification**

1. Run `python -m unittest test_testcase_gen -v`; add a focused standard-flow regression using repeated identical refs and assert no self-dependency while distinct sequence dependencies remain ordered.
2. Add generator tests asserting event-consistent request/response/status/flow selection, valid missing-prerequisite semantics, rejection of self-dependency and unsupported status claims, COD/card applicability, deterministic ordering, and safe/accurate classification of the wishlist-triggered request.
3. Exercise workbook generation with mocked rows and a temporary path; reopen with `openpyxl` and assert sheets/headers, URL/method/body/status, evidence and replay labels, complete flow references, setup/action/outcome consistency, and no truncated references or secrets.
4. Run `python testcase_gen.py --vault ./testVault --session 20260917T140421 --domain demowebshop.tricentis.com --debug` to inspect the event-bound contexts without model calls or workbook writes.
5. Against a controlled test instance only, run the cases that have safe fixtures and record actual status/body assertions. Mark unavailable or inconclusive cases `needs exploration`; do not run shared-demo order, vote, or other mutating actions.
6. Regenerate the workbook to a temporary output first, inspect both sheets and representative checkout/cart/security rows, then replace the repository artifact only after review. Confirm `RUNBOOK.md` commands and prerequisites match the delivered workbook.

**Decisions**

- Scope is generator fixes plus broader coverage and workbook regeneration, not a one-off spreadsheet-only edit.
- User selected controlled-environment checks; shared public-demo mutations are explicitly excluded.
- Unverified app behavior is not a pass/fail expectation: retain it as `needs exploration` until an observation from a controlled run supports a single deterministic assertion.
- The captured checkout has both `Payments.Manual` and `Payments.CashOnDelivery`; captured events differ (flow 08 vs. flow 14), so cases and status must be bound to the same selected event/method.
- The captured autocomplete response is empty and does not establish term-length thresholds. The wishlist click uses a cart-named endpoint and recorded response indicates a product-attribute error, so neither a positive wishlist assertion nor its classification should be inferred without verifying the triggering operation.
- Do not store test credentials or rely on captured session IDs/email addresses as reusable test fixtures.

# Switch Testcase Generation to Flow HTML

## Summary

Change `testcase_gen.py` so its normal run reads the flow HTML and writes only flow-derived testcases to `testcases.xlsx`. The selected source is `ObsidianVault/Reports/flow_visualizations/20261003T091516/full_flow.html`. The existing workbook’s two generated cases will be replaced.

## Implementation Changes

- Add a `--flow-html` option for the source file, defaulting to the selected HTML; keep `--out` for the workbook destination.
- Parse happy-path steps and every exploration branch card, including unmatched branches if present. Generate one end-to-end testcase for the happy path and one distinct testcase per branch. For this file, expect 1 happy-path case and 150 branch cases.
- Include the branch’s source step and connection quality in its testcase details. Use only outcomes shown in the diagram; leave hidden input values as explicit tester-supplied placeholders.
- Use the existing 11-column Excel format and execution fields. Stop adding API-, BRD-, or Knowledge-derived cases to this generator’s normal output. Update the testcase generation instructions in `RUNBOOK.md` for the new CLI.

## Validation

- Add parser coverage for happy steps, attached and unmatched branches, repeated actions, and escaped HTML text.
- Check this HTML yields 78 happy-path steps and 150 branches, then verify the workbook has 151 testcase rows, distinct branch cases, and the existing status and blank tester-entry fields.
- Confirm a missing or malformed flow file reports an actionable error without replacing the workbook.

## Assumptions

- The selected HTML is the default source, while `--flow-html` allows choosing another generated flow page.
- Unlinked API events are not exploration branches and do not produce testcases.
- The existing workbook layout stays unchanged; source traceability is included in testcase details rather than a new column.

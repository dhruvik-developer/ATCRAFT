# Proposal traceability records

`trace.json` is the original grouped proposal audit with selected later
work-package updates. `status.tsv` is its four-column view: requirement ID,
status, planned work package and requirement description. Each requirement is
one tab-separated row; embedded whitespace is flattened when regenerating it.
`gaps.md` and `notes.md` preserve the detailed findings and dated amendments.
These are historical audit records, rather than a live catalogue of the current
implementation. The original group notes identify the reviewed revision.

Do not infer that an old PARTIAL entry is still missing, or that an old PRESENT
entry proves a later release. Recheck the source, permissions, migrations and
tests before changing a requirement's status, and record the revision and
verification evidence with any amendment. Keep matching requirement IDs and
statuses in the JSON and TSV when updating them.

Use the [current documentation index](../../README.md),
[development standards](../../development-standards.md) and
[latest application verification record](../development-standards-20261022.md)
for current implementation guidance and dated checks.

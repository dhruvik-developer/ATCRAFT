# OpenCode continuation: 16 October 2026

Recovered checkpoint: OpenCode completed the staff-side E7b job-work package
through `9d2a0fd` on `develop`. Its last user instruction was to commit and push
directly to `develop`; the push timed out before the session reached its usage
limit. At takeover, the tree was clean and 16 local commits followed
`origin/develop` at `fe9cb41`. Existing UI-readiness changes were already included
in `55c9756`.

## Review corrections

- Bound challan status/download access to the internal creator, company and
  `JOB_WORK_CHALLAN` key, without granting general report/document rights.
- Kept `JOB_WORK_COST` exports behind cost permission on generic document reads.
- Split frozen receipt material value between good and returned rejected stock;
  charged processing only to good output; conserved quantity/value when rejected
  input is scrapped at the vendor. Cost reports now allocate per receipt.
- Exposed receipt/reconciliation loading and errors, corrected yield-based
  receive defaults, and added challan status/generation retries.
- Added the new detail route to the readiness resolver. Rework controls create
  clearly labelled drafts only from returned rejects and explain the current
  available-stock issue limitation.

## Verification

- Reports permissions, real worker PDF generation/download, and report-cost
  regressions: 76 focused tests passed.
- Receipt costing, rejection dispositions and existing job-work service:
  114 focused tests passed.
- Frontend job-work regressions passed, including 19 final detail/draft cases;
  TypeScript, formatting and lint checks passed.
- Production build passed; first load 198.3 KB gzip within the 210 KB budget;
  bundle credential scan passed.
- OpenAPI/Postman checks matched; one migration head, `20261016_1200`.
- Current production web build against the isolated UI-audit database: 21
  job-work screen/tab/viewport checks passed at 375, 768 and 1440 pixels.
  Artifacts are ignored under
  `tests/e2e/readiness-artifacts/resume-20261016/`.

Focused command counts overlap; they are not a unique full-suite test count.
Final commit/push retains the repository's pre-commit and pre-push checks.

## Retained scope limits

Vendor-portal/app job-work, optional ITC-04 export, and automatic rejected-output
charge-backs were excluded by the recovered session's task instruction. New
rework drafts preserve receipt linkage; issuing DAMAGED stock still needs a
separate reservation policy. The inventory engine's existing zero-cost fallback
is unchanged; explicit-zero valuation policy remains outside this patch.

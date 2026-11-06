# Release verification - 6 November 2026

This checkpoint records the release-hardening revision `1a57f59660209179a49d551086df653c6805a3bc`.
The full GitHub Actions run passed on `develop`:
[CI run 42213560921](https://github.com/dhruvik-developer/ATCRAFT/actions/runs/42213560921).
All five jobs completed successfully.

## Verification

- **Backend integration:** empty-database migration verification and 7,091 tests
  passed; combined statement/branch coverage was 81.74% against an 80% floor.
  Two warnings were reported.
- **Backend quality:** dependency-lock consistency, dependency audit, Ruff,
  mypy, OpenAPI, Postman and backend unit checks passed.
- **Frontend:** lint, formatting, type checking, contract checks, 1,616 tests
  across 124 files, and the production build passed. Bundle-budget and
  credential scans passed.
- **Release configuration:** Compose and deployment checks, fresh production
  image builds, and backend/frontend/edge-proxy vulnerability and secret scans
  passed.
- **Browser and operations:** the production-like stack passed Playwright
  journeys, baseline/demo seed idempotence, a real ClamAV EICAR detection check,
  registered-screen checks at mobile/tablet/desktop widths, and light/dark
  accessibility checks.
- **Focused local regressions:** 223 backend tests passed for MFA, upload
  scanning, document upload, dispatch notification, authentication and seed
  behavior. The frontend production build and generated API contract check
  passed locally. The pre-push suite also passed.
- **Handbook:** paced recapture completed for all 105 registered views with
  zero failures; the rebuilt PDF was visually checked for the updated MRP
  schedule settings.

The initial aggregate local check was interrupted during a very slow backend
test run and is not counted as a pass. The full isolated CI backend run above
completed successfully.

## Residual gates

- **Capacity evidence:** write-load testing still needs a populated, isolated
  target and one-use accounts. No write load was run against the shared
  development or test databases.
- **Production commissioning:** host/domain and TLS, off-box encrypted backup
  and restore evidence, production-sized load/recovery, restricted-role UAT,
  monitoring/on-call ownership and customer sign-off remain deployment gates.
- **Scanner operations:** deployed uploads fail closed through ClamAV. Operators
  must monitor daemon health and signature freshness; local development remains
  scanner-disabled.
- **External integrations:** live WhatsApp/SMS needs Meta/DLT accounts;
  courier automation needs a courier account.
- **DSC/PAdES:** implementation remains blocked on signer authority, certificate,
  integration and retention decisions.
- **Client policy:** mandatory MFA remains opt-in by configured role; valuation
  beyond FIFO, multi-currency and batch-lineage policy remain decisions for the
  business.
- **Public catalogue:** remains deliberately out of scope.

The E12 residual and current status are tracked in the
[E12 specification](./specs/E12-phase1-documents-cleanup.md) and
[implementation trace](./trace/status.tsv). Historical audit snapshots are
preserved and explicitly marked as superseded where current evidence differs.

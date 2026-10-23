# Repository synchronization - 24 October 2026

Current reference documents were checked against the application, migrations,
configuration, commands and generated API contract. The
[documentation index](../README.md) identifies maintained guides and historical
acceptance/review records. Historical results retain their original context;
this refresh does not relabel old findings as newly tested or certify a release.

## Corrections

- Updated setup, local seed, contribution and deployment instructions. Current
  integration uses `develop`; the deployment guard still requires `main` or
  `dev` for its corresponding deployment.
- Reconciled the database ownership map with all 135 ORM tables and corrected
  inventory grain, movement, reservation-consumption and financial guidance.
- Corrected descriptions of authentication, permissions, caching, metrics,
  company currency, invoices and the dealer invoice-projection limitation.
  Backend application edits are comments/docstrings; executable ASTs match the
  preceding revision.
- Aligned environment examples with configured defaults and supported push
  options, without changing private environment files. Dependency manifests
  and npm lockfiles agree; no dependency versions were changed.
- Regenerated OpenAPI, frontend types and Postman. Schema-description changes
  preserve all endpoint and request/response structures.
- Rebuilt the traceability TSV from its historical JSON source: all 477 IDs and
  statuses agree, with one four-column row per requirement. Restored UTF-8 text
  and repaired local source links.
- Updated handbook coverage, screenshots, HTML and PDF. Shared route/image
  helpers distinguish query tabs and preserve capture metadata. Capture blocks
  non-session writes, supports the configured API prefix and resolves RFQ
  details through the list's actual row navigation.

## Deployment and ongoing checks

`scripts/deploy.sh` now includes the notification worker in running-writer
detection, shutdown before migrations and application readiness before opening
nginx. Seven Bash behavior tests cover fresh/upgrade ordering, residual writers
and startup failure without deploying a real stack.

The shell-check hook now chooses Git Bash on Windows and checks every supplied
script separately; Bash otherwise treats later filenames as arguments to the
first script. Ten regression cases cover discovery, failure reporting and
syntax-only behavior. CI uses the same helper for release-script validation.

CI also runs handbook route/guard checks and nine permanent image-lookup
regressions. Dependabot covers the E2E npm manifest as well as the frontend.

## Verification

| Check | Recorded result |
|---|---|
| Focused backend deployment, shell, cleanup, host/config and dealer regressions | 102 passed |
| Handbook helper regressions | Nine passed; no failures or skips |
| Handbook coverage and capture guard | 70 views, 58 application routes, three public routes; 377 non-session write operations blocked |
| Full fresh handbook browser capture | 70/70 views, zero failures; isolated populated snapshot |
| Generated handbook | 165 pages; all views illustrated; rendered review and page-bound checks passed |
| Backend lint/format | Ruff and formatting passed |
| Frontend static checks | ESLint, TypeScript, Prettier and generated types passed |
| Production web build | Passed bundle budgets and credential scan across 187 assets |
| Configuration | Compose parse, Git hook configuration and every release shell script passed |
| Documentation/data | Local Markdown links resolve; tracked JSON parses; traceability rows agree; tracked text is UTF-8 |

The full pre-commit and pre-push gates remain required for the commit and push.
The earlier complete backend/API, responsive, accessibility and business-flow
results remain in the [22-23 October verification](development-standards-20261022.md).
The snapshot capture is a documentation check, not a substitute for those gates.

The isolated API/web processes and disposable database were removed. Source
users/passwords, configuration and storage fingerprints were preserved. Private
test evidence and the preceding image set were retained outside the checkout.
The generated handbook PDF, HTML and screenshots remain Git-ignored; their
maintained content, builders, helpers and tests are versioned.

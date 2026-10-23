# Application standards verification — 22–23 October 2026

This review applies the shared [development standards](../development-standards.md)
to the existing FastAPI and React application. Existing business behavior and
uncommitted work were retained. The changes establish common contracts and
completion checks; they do not certify every function as fully tested or close
unrelated proposal and deployment findings.

## Implementation

- API routers use typed success, page, cursor and error envelopes, shared query
  validation and documented HTTP status conventions. OpenAPI, frontend types
  and Postman are checked together; downloads retain their binary contract.
- Validation, expected business failures and unexpected exceptions share safe
  messages and request correlation. Malformed JSON returns 400; schema failures
  return 422. Database lock/transaction conflicts return 409. Statement timeouts,
  connection and pool failures return 503 without disclosing SQL or driver text.
- Request IDs are bounded and validated before logging or auditing. Client IPs
  are validated. Streamed request bodies enforce the size limit even when the
  Content-Length header is missing or false; partial uploads and cancelled
  request contexts are cleaned up.
- Frontend forms, lists, downloads and notifications use shared messages and
  safe error descriptions. Unexpected query, mutation and render errors use
  internal reporting and a common user-facing fallback; direct request
  notifications also hide technical exception text. Query retries distinguish
  transient transport errors from programming errors, and list requests
  propagate cancellation.
- Shared UI tokens, card headings, planning tab/panel relationships and toast
  roles were corrected. Light and dark colors were adjusted where browser
  accessibility scans found insufficient contrast.
- Browser checks now fail on API/network/render failures and visible error
  notifications, including an invalid response delivered with HTTP 200. This
  prevents a failed widget from counting as a successfully loaded screen.
- Sales fulfilment now reads the mapped MRP availability-date field, with HTTP
  regressions for dated and legacy-null suggestions. Its joined query uses
  typed SQLAlchemy tuples so static checking catches invalid mapped fields.
  Dashboard activity uses the audit endpoint's cursor envelope.
- Web-push truncation terminates even when Unicode metadata alone exceeds the
  payload limit. Browser and provider replacement topics stay consistent;
  regression probes run with an independent deadline.

Non-obvious rules are documented at their implementation points. Naming follows
the existing Python, TypeScript, HTTP and database conventions rather than
renaming persisted entities or public paths for cosmetic consistency.

## Migration and schema agreement

The new forward revision is
[`20261027_0100_schema_contract_alignment.py`](../../apps/api/migrations/versions/20261027_0100_schema_contract_alignment.py).
The verified baseline and existing revisions remain unchanged. It adds ten
timestamp defaults and nine indexes, aligns five tenant foreign keys with the
shared RESTRICT deletion policy and renames 24 equivalent CHECK constraints.
The existing QC-decision creation-time index is explicitly represented in the
model rather than removed.

Concurrent index builds run outside the metadata transaction and recover invalid
indexes left by an interrupted build. Metadata changes use a five-second lock
timeout; foreign-key validation still scans the affected tables and must be
scheduled with normal migration review. CHECK renames preserve their identities,
expressions and validation state. The downgrade restores the previous contract.

The fresh-baseline test passed. The new integrated migration regression also
passed, exercising missing-default behavior before upgrade, interrupted-index
repair, all five deletion guards, CHECK identities, exact downgrade, preserved
data, re-upgrade, offline SQL generation and an unfiltered `alembic check`.
The final migrated schema has no pending model operations.

These checks used disposable databases. The existing development database was
neither migrated nor reset during this review.

## Quality gates

`make check` / `.\make.ps1 check` now runs contract checks, backend/frontend
lint/format/type checks, backend tests with coverage, frontend tests and the
production web build. The build checks bundle budgets and bundled credentials.
CI records backend branch coverage and runs accessibility checks across all
static screens in both themes, retaining the reports even when a check fails.

The backend gate includes startup and route wiring and enforces an 80% combined
statement/branch coverage floor. The final complete run measured statement
coverage of 84.50%, branch coverage of 64.81% and combined coverage of 81.08%.
This is a measured baseline, not complete function or branch coverage.
New tests must exercise behavior, errors and boundaries rather
than merely repeat implementation details.

## Verification record

| Check | Result |
|---|---|
| Backend final full unit/integration/API suite with branch coverage | 5,868 passed, zero failures or skips, in 1,866.32 seconds; 81.08% combined coverage passed the gate |
| Backend static checks | Ruff and formatting passed across 576 files; mypy passed for 258 source files |
| Fresh migration baseline and schema-contract regression | Both passed independently and in the final complete suite |
| OpenAPI, Postman and frontend contract checks | Passed |
| Frontend final complete serial suite | 1,222 passed across 93 files, zero failures, in 631.32 seconds |
| Frontend static checks and production build | ESLint, TypeScript and Prettier passed; build, bundle budgets and credential scan passed |
| Production assets | First load 200.8 KB gzip; heaviest screen 37.2 KB gzip; 187 assets scanned |
| Populated responsive checks | 58 registered routes; 402 screen/tab/viewport checks, 195 forms opened, zero failures across 375/768/1440 pixels |
| Accessibility | 50 screens in light mode and 50 in dark mode; zero rule violations or load failures |
| Complete browser journeys | 17 passed, zero failures or skips, in 42.2 seconds |
| E2E dependency audit | Zero vulnerabilities reported |
| Local seed recovery/idempotence | All 136 table counts, ledger/summary contents, 11 file hashes and all password hashes stable on rerun; four PDFs complete; fresh CLI regression included in the backend suite |

The earlier full backend run had 5,734 passing tests and one readiness timing
failure: first-use FastAPI route preparation consumed the dependency deadline.
The regression now warms the liveness route before measuring readiness and
asserts that the timed-out dependency is cancelled. The configured readiness
deadline was preserved; focused reruns passed.

The earlier frontend run had 1,216 passing tests and two MRP failures under
serial suite load. Both tests put two dependent initial requests under one
default asynchronous assertion deadline. They passed unchanged in isolation;
the tests now wait for the parent run heading and dependent list request before
checking the same rows and conversion payload. No timeout was increased, and
the focused MRP and new toast regressions all passed.

Both final complete suites include these corrections and passed without skipped
tests or relaxed deadlines. The backend reported two upstream deprecation
warnings from the HTTP client/test stack. Frontend coverage percentage was not
measured; a passing test count is not a claim of complete function coverage.

Final backend evidence is in the temporary
`atcraft-backend-final-20261023.log`,
`atcraft-backend-final-20261023-coverage.xml` and
`atcraft-backend-final-20261023-coverage.json` files.
Frontend evidence is in `atcraft-web-standards-clean-full-tests.log`,
`atcraft-web-standards-final-static.log` and
`atcraft-web-standards-final-build.log`. These local generated files are outside
the repository; CI retains fresh coverage and browser reports on its own runs.

## Browser environment and limits

The local browser runs used an independently named snapshot of the populated
development data, migrated before use, an isolated API on port 8001 and a web
server on port 5174. The API used independent authentication settings, isolated
storage, no external notification delivery and no running background workers.
Browser writes were confined to the clone. The normal development API, web
server and database were retained.

Readiness checks discover registered routes, resolve populated detail pages and
exercise top-level tabs and primary forms at 375, 768 and 1440 pixels. They do
not submit every lifecycle dialog. The forced-password screen uses a fresh
browser session with the current test identity marked as requiring a password
change; it does not change the stored account password. Accessibility scans use
the static screens plus login in both themes.

The final responsive report combines 134 completed checks at each width. Phone
and tablet entries came from the initial guarded run; a Vite reload caused by
a test-file edit invalidated its later desktop entries. Those failed desktop
entries were discarded and replaced by a complete guarded desktop retry after
all frontend source and test edits were frozen. The combined report identifies
both source reports; no failed or incomplete entries count as passing checks.

These are local application checks. They do not replace a fresh production-image
build, current container/dependency scans, worker and notification delivery
verification, restricted-role business acceptance, or the remaining acceptance
criteria in the [production-readiness review](production-readiness-20261017.md).
The populated clone was subsequently upgraded from `20261026_0100` to
`20261027_0100`. An unfiltered `alembic check` passed, all 136 public-table row
counts were preserved, and readiness, authenticated identity and item-list HTTP
checks returned 200. This did not change the source development database.

The user subsequently requested a comprehensive local seed command and a commit
and push to `develop`. The [local seed guide](../local-seed-data.md) documents the
single entry point, supported fixture coverage and safe repeat behavior. The
seed work also corrected physical counts checking completion before flushing
their entered quantities, and added explicit tenant targeting for shared
administrator emails. Derived stock summaries and invoice/credit PDFs are
prepared without external workers; failed PDF attempts use normal regeneration
services. No deployment or release sign-off was performed.

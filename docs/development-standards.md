# Development and completion standards

These standards apply to existing modules and new work. The binding engineering
rules remain in [CLAUDE.md](../CLAUDE.md); the
[database contract](database.md) defines ownership and service boundaries.
Refactor through the existing shared implementations and preserve documented
business behavior, permissions, API compatibility and transactional guarantees.

## Shared implementation points

| Concern | Implementation and rule |
|---|---|
| HTTP contracts | `apps/api/src/core/responses.py`, `errors.py`, `exception_handlers.py` and `validation.py`; use typed envelopes, stable error codes, field errors and the status conventions in [api.md](api.md). Regenerate OpenAPI, frontend types and Postman together. |
| Request boundaries | Pydantic request/query schemas, route permission dependencies and shared middleware. Bound input sizes and pagination; derive tenant and portal scope from the verified principal. File downloads retain their binary contract. |
| Business rules | Thin routers, services for authorization-sensitive business decisions and transactions, tenant-scoped repositories for persistence. Lock and re-read mutable inventory; keep movements, document transitions and audit writes atomic. |
| UI system | `apps/web/src/styles/globals.css` and `src/components/ui`; reuse token colors, typography, spacing, buttons, fields, dialogs and overlays. Lists use `DataTable` and `useListQuery`. |
| Messages and errors | Backend `core/messages.py`; frontend `lib/messages.ts`, `form-errors.ts`, `toast.ts` and `api-client.ts`. Show safe API messages and field errors; unexpected failures use a shared fallback and internal reporting. Preserve request correlation without exposing exception text, SQL, credentials or stack traces. |
| Data loading | TanStack Query with shared `queryKeys`; forward cancellation signals, paginate on the server and preserve loading, empty and failure states. Bound retries and avoid duplicate requests or notification storms. |
| Security and performance | Follow [security.md](security.md) and [performance.md](performance.md). Batch reference queries, select only needed data, close resources and send long calculations to workers. Never cache inventory availability. |

Use Python `snake_case` for modules/functions/variables, `PascalCase` for classes
and `UPPER_SNAKE_CASE` for constants. Frontend files follow the existing
`kebab-case` convention, components/types use `PascalCase`, hooks use `use...`,
and values/functions use `camelCase`. Database tables are plural `snake_case`;
HTTP paths follow the established resource names. A naming cleanup must update
callers and generated contracts without silently breaking persisted data or
public URLs.

Document non-obvious algorithms, locking order, permission-dependent responses
and business assumptions at the decision point. Record changed assumptions in
[assumptions.md](assumptions.md); comments that restate an assignment add no value.

## Verification required for completion

Review every affected function, method, component and endpoint for applicable
tests. Cover successful behavior, boundary and malformed inputs, validation and
business-rule failures, authorization/tenant isolation, expected exceptions and
unexpected failures. For asynchronous code, include cancellation, cleanup and
retry behavior where applicable. A shared helper needs behavioral regression
tests and representative caller coverage.

Database constraints, locking, rollback, concurrent inventory updates and
cross-module workflows require PostgreSQL integration tests. HTTP tests must
check status, envelope, permission checks and safe error content. React tests
must exercise user-visible loading, validation and failure states as well as
successful interactions. Browser checks cover real layout, focus and responsive
behavior; DOM tests alone cannot prove these properties.

Fresh-install and migration tests must also run an unfiltered `alembic check`:
schemas rebuilt from ORM metadata do not prove that deployed migrations match
the models. Keep deployed migration history immutable and reconcile drift through
reviewed forward migrations, including locking, retry and downgrade behavior.

Keep the [local seed entry point](local-seed-data.md) aligned with supported
workflows. Verify it on a newly migrated disposable database and repeat it:
business document counts, ledger entries and stored files must not duplicate.
Use service transitions for stock and money; never fabricate final statuses or
edit a real database to make a fixture pass. A custom company must be selected
explicitly when the same administrator email exists in multiple tenants.

Run from the repository root:

```sh
make check       # Windows: .\make.ps1 check
make test-cov    # Windows: .\make.ps1 test-cov
```

`check` runs contract checks, lint/format/type checks, backend unit/integration/API
tests, frontend tests and the production web build. The build also enforces
bundle budgets and scans bundled assets for credentials. `test-cov` measures
backend statement and branch coverage, including application startup and route
wiring. The completion gate and CI enforce an 80% combined statement/branch
coverage floor for the full backend suite; CI retains `coverage.xml`.
Read uncovered branches and add meaningful tests; a passing suite or a coverage
percentage alone does not prove every important scenario is tested.

Run `make test-e2e` and the populated-screen checks described in
[ui-readiness.md](ui-readiness.md) for affected browser flows. Check mobile,
tablet and desktop widths, keyboard focus, scrolling, long content, validation
and dialogs. A failed API, redirected page, missing fixture or unavailable
service is an unresolved check, never evidence that a screen passed.

CI additionally verifies dependency locks and audits, release configuration,
migrations, container builds/scans and production-stack browser journeys. Local
`check` covers the application checks; these deployment checks remain required
for release. Keep the [pre-commit/pre-push hooks](../.pre-commit-config.yaml)
enabled and fix failures without weakening rules or bypassing checks.

Record the commands, results and any blocked verification when handing work over.
Existing production-readiness findings remain open until their own acceptance
criteria are met; a standards refactor does not close unrelated business or
deployment gaps.

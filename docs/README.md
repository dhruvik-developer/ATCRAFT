# Documentation index

The current implementation is defined by the source, migrations and generated
API contract. The guides below describe that implementation; the repository's
binding engineering rules are in [CLAUDE.md](../CLAUDE.md).

| Guide | Purpose |
|---|---|
| [Project README](../README.md) | Installation, local URLs, commands and contribution workflow |
| [Development standards](development-standards.md) | Shared code patterns, testing and cleanup requirements |
| [Architecture](architecture.md) | Application layers, module boundaries and background work |
| [Database contract](database.md) | Table ownership, stock and financial service boundaries |
| [API conventions](api.md) | Validation, response envelopes, errors and generated clients |
| [Security](security.md) | Authentication, permissions, tenancy, secrets and file handling |
| [Performance](performance.md) | Database access, caching, workers and measurement |
| [Local seed data](local-seed-data.md) | Repeatable local demonstration dataset and credentials setup |
| [UI readiness](ui-readiness.md) | Browser checks, responsive coverage and dated verification evidence |
| [Client handbook](handbook/README.md) | User workflows and rebuilding the illustrated guide |
| [Deployment](deployment.md) | Release configuration and operational checks |
| [Host runbook](../ONE-BOX-RUNBOOK.md) | Two isolated deployments, TLS, backups and recovery |
| [Business assumptions](assumptions.md) | Explicit business policies and unresolved confirmations |

## Generated files and configuration

`make contract` (Windows: `.\make.ps1 contract`) regenerates
`openapi.json`, `apps/web/src/types/openapi.ts` and `postman_collection.json`.
`contract-check` verifies all three against their sources. Dependency manifests
and lockfiles are versioned together; upgrading dependencies is a separate
reviewed change, rather than part of a documentation refresh.

The local `.env.example` and `deploy/*.env.example` describe different execution
environments. Populate private copies for your installation; keep real
credentials out of documentation and source control. `make clean` removes
generated test/build outputs while preserving private configuration, stored
documents, backups, dependencies and handbook deliverables.

## Historical records and acceptance specifications

[Implementation records](implementation/README.md) preserve dated reviews,
test results and proposal evidence. Results describe the revision and environment
recorded in each report; they do not certify a later release. The
[work-package index](implementation/specs/SPEC-INDEX.md) describes acceptance
requirements, while lane plans, research reports and the original traceability
audit retain their historical context. Use current source and the guides above
to confirm whether an older finding still applies. Superseded root planning
documents live in the [archive](implementation/archive/BUILD-ORDER.md).

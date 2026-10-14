# WP E12: Phase-01 deliverable documents and dead-code cleanup

Lane A, round 7, step 2. Assumption placeholders use the prefix `§E12-n`.

**Checklist IDs:** P01.D5 (security issue resolution report), P01.1 (legacy review record), P01.2 (dead code), P01.D6 (production deployment checklist).

Read each ID in `trace/trace.json` first. Several need a document, not code.

## Decisions (binding)

### 1. Security issue resolution report: `docs/security-issue-resolution-report.md`

- Write one row per Phase-01 issue (P01.3–P01.13), with these columns:
  - the finding (legacy behaviour as stated in the proposal: hardcoded bypass token, MD5, plain-text passwords, unauthenticated APIs, no JWT expiry, secrets in code, missing RBAC/audit, error handling, response format)
  - the resolution (module/function/file)
  - verification (test names, the route-permission scan figures: rerun the AST scan of all route decorators and report the current counts)
  - status
  - residual risk
- Include the issues found and fixed during this programme:
  - vendor isolation (C1a allow-list)
  - dealer isolation (C3a)
  - the vendor-invoice cancel reversal
  - the T7 strand
  - cross-tenant attribute ids on item create
  - the A1 race
  - Smart App Control hooks
- Include open items: malware scanning of uploads (known gap), `pip-audit` / `npm audit` results (run them **read-only** and record the output summary), DSC signatures.
- Add a **"Legacy system review"** section (P01.1) mapping each legacy module (S02.1–S02.15) to its replacement here (module, route, screen), or to "planned" / "dropped".
- Format it to export cleanly with the handbook tooling. Add a short PDF build note.

### 2. Production deployment checklist (P01.D6)

- Confirm where the checklist lives today (`ONE-BOX-RUNBOOK.md` / `docs/deployment.md`).
- Make sure it is one explicit, ordered checklist covering:
  - secrets
  - HTTPS
  - backups
  - migrations
  - seed
  - the new settings (web push, WhatsApp/SMS, courier, public catalogue, auto-plan, MRP schedule)
  - Celery queues (`-Q default,mrp,reports` + `notifications`)
  - beat
  - health checks
  - first-login steps
  - the role grants an administrator must make for client-owned roles. List every new permission added in this programme per preset.

### 3. Dead code (P01.2)

- Remove or wire every item the audit lists. Most are wired by now: approvals (B4), `qc_required` (E5), `customer_price_type`. Verify each one.
- Delete only what is truly unused. Prove it with a grep, and record each removal in the commit body.

## Tests

- Run the full unit suite after the cleanup.
- Run the docs link check if one exists.

# Production closeout checkpoint — 8 November 2026

> Supersedes the “remaining software” framing in
> [`production-scope-20261025.md`](production-scope-20261025.md) for in-repo
> delivery status. That document remains the package audit trail; this file is
> the dated ship checkpoint after the fresh Alembic baseline.

## Verdict

**In-repo software is ready for commissioning.** The single-app migration head
is `20261127_0100` (see
[`migration-squash-20261127.md`](migration-squash-20261127.md)). Demo seed via
`.\make.ps1 seed-local` / `db-fresh` covers masters through documents, including
courier webhook account `DM-COURIER-WH`, persisted company approval settings,
collections follow-ups, dual-PO thresholds, and dealer/vendor portal flows.
Deploy examples default `LOG_JSON=true` for live structured logs.

This is **not** a claim that a production host is live. External gates below
remain operator/customer work.

## Explicitly deferred (external / out of application)

| Item | Disposition |
|---|---|
| D4 DSC/PAdES | Deferred — legal/signing model + certificates ([memo](d4-dsc-pades-compliance-20261019.md)) |
| Vendor native mobile app | Deferred — not in this application |
| Live Meta / DLT / Shiprocket | Config + customer accounts; code present, disabled by default |
| Host TLS, off-box backup restore, restricted-role UAT | Operator commissioning via [`ONE-BOX-RUNBOOK.md`](../../ONE-BOX-RUNBOOK.md) and [`deployment.md`](../deployment.md) |
| Batch SKU transformation / some serial edges | Documented open boundaries in production-scope; leave as-is |

## What shipped in this closeout pass

- Fresh single Alembic baseline `20261127_0100` (tag `pre-migration-squash-20261127`)
- Demo seed gaps closed for courier admin + approval settings + collections coverage docs
- Live logging defaults (`LOG_JSON=true`) and runbook “Live debugging” notes
- Contract artifacts refreshed to HEAD (`make contract`)

Mark historical rows in `production-scope-20261025.md` that claimed an open
in-repo gap for D2/D3/A4/B4 as delivered; only the external gates table above
remains open for go-live.

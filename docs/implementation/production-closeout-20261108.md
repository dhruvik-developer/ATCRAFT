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
| Vendor / dealer **store-native** apps (App Store / Play) | Deferred — installable PWA at `/vendor/*` and `/dealer/*` is the shipped mobile surface |
| Live Meta / DLT / Shiprocket | Config + customer accounts; code present, disabled by default |
| Host TLS, off-box backup restore, restricted-role UAT | Operator commissioning via [`ONE-BOX-RUNBOOK.md`](../../ONE-BOX-RUNBOOK.md) and [`deployment.md`](../deployment.md) |
| D4 certificates and legal signing model | Deferred — see d4 memo |

## What shipped in this closeout pass

- Fresh single Alembic baseline `20261127_0100` (tag `pre-migration-squash-20261127`)
- Demo seed gaps closed for courier admin + approval settings + collections coverage docs
- Live logging defaults (`LOG_JSON=true`) and runbook “Live debugging” notes
- Contract artifacts refreshed to HEAD (`make contract`)
- Vendor/dealer installable PWA (install-to-home card + Chromium `beforeinstallprompt`)
- Standing MFA / approval / serial / partial-dispatch / FIFO assumptions recorded
- One-to-one batch SKU conversion lineage (`stock_batch_conversions`, head `20261128_0100`)

Mark historical rows in `production-scope-20261025.md` that claimed an open
in-repo gap for D2/D3/A4/B4 as delivered; only the external gates table above
remains open for go-live.

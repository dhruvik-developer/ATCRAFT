# WP D4 — Digital signature on purchase orders
Lane B, round 5, step 2 (after D1). Placeholder `§D4-n`.

Proposal: Future Enhancement "Digital signature on PO" — client now wants it present.

Approach (`§D4-1`, binding): an **application-level digital signature** — Ed25519 over a canonical digest of the
approved PO — embedded in the PO PDF as a visible signature block plus a QR code that opens a public verification page.
It is tamper-evident and verifiable by the vendor without an account. No new dependency: Ed25519 from `cryptography`,
QR from `reportlab.graphics.barcode.qr`. A certificate-based (DSC / PAdES) signature is the documented upgrade path: it
needs the client's Digital Signature Certificate (usually a USB token) and a PDF-signing library — to be justified as a
dependency if Atcraft asks for it.

## Decisions (binding)
1. **Keys**: per-company Ed25519 key pair derived deterministically from `CREDENTIAL_ENCRYPTION_KEY` with HKDF info
   `"po-signature:" + company_id` (no new secret; key id = first 16 hex of SHA-256(public key)); `GET
   /procurement/signing-key` (`purchase_order.view`) returns the public key + key id for auditors. Key rotation follows
   the root key; old signatures keep verifying via a stored public key on each signature row.
2. **Table `purchase_order_signatures`** (ImmutableLedgerEntity): `purchase_order_id`, `revision` (1..n — a PO amended
   after signing gets a new signature; D1's increase-quantity amendment re-approves → re-signs), `digest_sha256` (hex),
   `signature` (base64), `public_key` (base64), `key_id`, `signed_at`, `signed_by_id`, `signer_name`,
   `verification_token` (opaque random, unique, 22+ chars url-safe; NOT the PO id), `superseded_at` NULL.
3. **Canonical digest** (pure, unit-tested, versioned `SIGNATURE_FORMAT = "atcraft-po-v1"`): JSON with sorted keys and no
   whitespace of: format, company legal name + GSTIN, PO code, PO date, vendor code + name + GSTIN, warehouse code,
   currency, each line (line no, item code, description, HSN, qty, UoM code, unit price, discount, tax rate, tax, total —
   Decimals as normalised strings), header totals, payment terms, terms text, approved_by name, approved_at (UTC ISO).
4. **When**: signing happens in the same transaction as PO **approval** (PENDING_APPROVAL→APPROVED) — the approver is
   the signer. Existing approved POs are signed lazily on first PDF generation (by the system, signer = original approver
   name; ASSUMPTION). Any later change to signed content (D1 amendments) supersedes the old signature and the PO must be
   re-approved, which re-signs.
5. **PDF**: the PO renderer (C1a) adds a signature block: "Digitally signed by <signer> for <Company legal name> on
   <date time IST> · Key <key id> · Fingerprint <first 16 of digest>" and a QR code encoding
   `<PUBLIC_WEB_URL>/verify/po/<verification_token>`; plus the full URL printed small.
6. **Public verification**: unauthenticated `GET /public/verify/purchase-order/{token}` → `{status: VALID | MODIFIED |
   SUPERSEDED | UNKNOWN, po_code, po_date, company_name, vendor_name, total, currency, signed_by, signed_at, key_id,
   fingerprint}` — VALID only if the stored signature verifies against the stored digest AND the digest recomputed from
   the current PO equals it (else MODIFIED); SUPERSEDED when a newer revision exists. Exposes no line prices beyond the
   total (ASSUMPTION — say so). Rate limited by the existing flood guard; constant 404 shape for unknown tokens. Add to
   auth exemptions like other public routes.
7. **Frontend**: public page `/verify/po/:token` (no app shell; plain verification card with a big status badge) — add
   to sweeps (ungated) ; PO detail shows a "Digitally signed" badge with signer/time/fingerprint and the revision history;
   vendor app PO detail shows the same badge.
8. **Docs**: security.md (what the signature proves and does not — not a DSC; key derivation; verification page),
   assumptions (`§D4-n`), handbook (how a vendor verifies a PO; QR), api.md.

## Tests
Unit: canonical digest stability (key order, Decimal normalisation), sign/verify, tamper → MODIFIED, supersede logic,
key derivation determinism per company and difference across companies. Integration: approval signs in the same
transaction (rollback leaves no signature), lazy signing for legacy approved POs, verification endpoint statuses,
amendment supersedes (with D1). PDF: renders with QR (bytes contain the verify URL text). Frontend: verify page states.

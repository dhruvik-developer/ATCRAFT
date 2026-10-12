# Lane C round 2 completion checkpoint

C1a vendor portal and C1b Web Push backend are implemented on lane/portals from
fd90655. No main merges, deployment, dependency installation or shared env edits.

## Delivered

Structural vendor route allow-list; five portal rights and exact vendor role
presets; vendor-scoped recipient resolution; vendor facade with all scoped PO
and finance reads/actions; separate committed date; declined lifecycle; numbered
consignments, delivered evidence, typed files and invoice attachment links;
staff companions; shared Python/SQL tracking rules and late predicate; queued
permanent PO PDF; transaction-bound vendor/buyer notifications; eight demo cases.

Push subscription ownership transfer, encrypted URL storage and SSRF host gate;
RFC AES128GCM/VAPID implementation without a new dependency; sparse compatible
preferences; outer-commit staging; claim/send/update worker, retry release and
GONE cleanup; 120-second email/push recovery; account/session cleanup; six APIs;
settings, nginx PWA paths, docs and regenerated contract/types/Postman.

## Migrations and contracts

Single lane head: 159ffb5b9cb4 -> c1a1100scope -> 4480835a545e -> c1bpush2026.
The first revision replaces five system-role permission arrays and extends
ADMIN/SUPER_ADMIN. The second adds nullable PO metadata, shipments and typed
attachment links, and literal CHECK swaps for VENDOR_DECLINED/PURCHASE_ORDER.
The third adds push preferences/marker/device table and recovery indexes.
Fresh database upgrade to head, downgrade to 159ffb5b9cb4, upgrade to head passed;
the temporary verification database was removed. A populated lane also underwent
the consignment/push roundtrip before demos. Live vendor system-role arrays match
all five presets. Migration lock/rollback notes are in each revision.

Rights: vendor_portal.access, respond, ship, invoice, quality. OWNER has all;
MANAGER access/respond/ship; DISPATCH access/ship; QC access/quality; ACCOUNT
access/invoice. Every preset also has notification.view. AUDITOR does not absorb
these rights.

New vocabularies: PO VENDOR_DECLINED; report PURCHASE_ORDER; channel PUSH; eight
portal notification types; eight exact inbound tracking statuses. New settings:
WEB_PUSH_ENABLED=false, optional WEB_PUSH_VAPID_PRIVATE_KEY, WEB_PUSH_SUBJECT,
WEB_PUSH_TTL_SECONDS=86400, WEB_PUSH_TIMEOUT_SECONDS=10, and the six-provider
WEB_PUSH_ALLOWED_HOSTS suffix list. Dev overlay enables push.

Contract contains 556 requests in 28 Postman folders. OpenAPI/Postman and generated
frontend types are synchronized. Docs cover security, schema/events, architecture,
assumptions C1-1..C1-10, runbook, APIs and inbound handbook preparation.

## Verification actually run

- Initial targeted units: 112 passed; crypto/worker/role focused run: 55 passed.
- One full unit run: 3137 passed, 1 existing skip, 5 failures. Three obsolete/default
  expectations and the overlong FK naming defect were corrected; 91 affected
  tests then passed. No claim that the full suite was rerun green.
- Final focused unit set: 135 passed.
- Final grouped HTTP/PostgreSQL integration set: 56 passed. Includes real auth
  vendor isolation, device takeover/opt-out/collapse/logout cleanup, foreign
  PO/file/invoice hiding, shipments, typed downloads, invoice review, GRN/QC,
  approved payments and allocations, and notification service regression coverage.
- Latest legacy tracking/shared-rule and requisition release checks: 25 passed.
- RFC 8291 Appendix A ciphertext is byte-for-byte exact. VAPID signature/claims,
  SSRF rejection, payload byte budget, post-commit staging, worker transaction
  boundaries and compare-release SQL, outcomes, beat registration and confidential
  failure logging are covered. Fake transport only; no real provider calls.
- Backend Ruff lint/format and mypy (223 source files) pass. Frontend TypeScript,
  ESLint and changed-source Prettier checks pass. Compose production helper and
  development overlay parse checks pass with isolated placeholder env values.
- Live seeded PO data extraction and PDF rendering pass (one line, 2441 bytes).
- Demo masters/procurement run succeeds; rerun creates zero records. Existing
  serial-receipt limitation remains an explicit demo skip.

## Integration notes and limits

Regenerate merged OpenAPI/types/Postman instead of manually reconciling generated
files; recount docs/api.md. Merge/rebase the three Alembic revisions onto the final
combined chain. The new short FK is fk_po_documents_shipment. Expect shared-file
conflicts in procurement/finance/quality notifications, permissions, demo hooks,
reports and docs. Commercial A2 may provide a common Indian-money helper; this
lane's local indian_money is ready to consolidate.

C2 supplies vendor/inbound screens and the actual manifest/service worker. The
handbook PDF/screenshots were not rebuilt for nonexistent C2 screens. No external
provider delivery, real browser push subscription or live Celery/SMTP/broker-outage
exercise was claimed. All worker provider tests use fake transport.

Invoice files use a deliberate two-step API: upload INVOICE on the owned PO,
then pass invoice_document_id when submitting the bill. Existing legacy PO
courier fields remain tracking evidence until a numbered consignment exists;
no historical shipment rows are invented. Detail embeds first 50 records with
pagination metadata. Claimed delivery rows retain the specified crash window;
recovery sweeps recover unclaimed rows, not a worker killed after a claim.

Future dealer integration must extend the structural identity gate and internal
recipient exclusion. Current schema has vendor identity only. New push routes are
self-scoped and available to both staff and vendor users.

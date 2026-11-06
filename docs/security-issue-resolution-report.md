# Security issue resolution report

Prepared 2026-11-05 for the current development revision. This report closes
P01.1, P01.2 and P01.D5 in the implementation trace. The legacy application
source was not supplied; this is a review of the current rewrite and the
findings captured in the original proposal, not a source-to-source comparison.

## Phase-01 findings

| ID | Finding | Resolution | Verification | Status | Residual risk |
|---|---|---|---|---|---|
| P01.3 | API authentication was absent or inconsistent. | Central principal resolution, Argon2id credentials, signed access/refresh tokens, rotating HttpOnly refresh cookies, CSRF binding, MFA, lockout and per-request authorization in `apps/api/src/core/security.py`, `core/deps.py` and `modules/auth/`. | `test_auth_cookies::test_refresh_credential_is_http_only_and_scoped_to_auth`; `test_auth_mfa::test_mfa_challenge_is_short_lived_and_password_authenticated`; `tests/integration/test_auth_service.py`. | Resolved | MFA enforcement policy remains a client deployment decision. |
| P01.4 | JWTs had no enforced expiry. | Access and refresh expiry are configured with bounded settings, included in tokens and validated during decode; password changes invalidate earlier sessions. | `test_auth_session_state::test_expired_session_is_expired`; `test_auth_mfa::test_mfa_challenge_is_short_lived_and_password_authenticated`. | Resolved | Operators must protect and rotate deployment signing secrets. |
| P01.5 | A hardcoded bypass/master token could grant access. | No bypass token remains in API or web source; permissions are resolved from the verified principal and role grants. The current route inventory is summarized below. | Source search; `test_users_permissions::test_roles_are_unioned`; `test_role_presets_reach_roles::test_the_superuser_preset_holds_every_permission`. | Resolved | None identified in the reviewed source. |
| P01.6 | Secrets were embedded in source or accepted from unsafe templates. | Typed environment-backed settings, deployment secret validation, ignored private env files, and secret-detection hooks. | `test_env_template::test_every_setting_has_a_template_line`; `test_deployed_secret_placeholders::test_placeholder_detection_covers_short_weak_and_marked_values`; `test_deployed_host_config.py`. | Resolved | Real secrets, hostnames and provider credentials remain deployment-owned. |
| P01.7 | Plain-text passwords were used or stored. | Passwords are stored as Argon2id hashes. Bootstrap passwords are randomly generated, printed once, and require a change; reset tokens are stored as digests. | `test_password_hashing_offload::test_the_async_hash_is_verifiable_by_both_forms`; `tests/integration/test_users_service.py`. | Resolved | Bootstrap output must be handled as a secret and rotated at first login. |
| P01.8 | MD5 was used for password hashing. | New hashes use Argon2id; legacy bcrypt hashes are verify-only and upgraded on successful authentication. No MD5 implementation remains in API source. | `test_auth_credentials::test_dummy_hash_is_argon2id_so_the_verify_costs_what_a_real_one_costs`; `test_password_hashing_offload::test_the_async_verify_agrees_with_the_sync_one`; source search for MD5. | Resolved | Legacy MD5 credentials cannot be verified; affected users require password reset. |
| P01.9 | Unauthenticated APIs were not protected. | Only the intended authentication endpoints are public and rate limited. The courier callback verifies its configured HMAC or API key. Deployed docs, metrics and health paths are blocked at the public edge; document downloads use authenticated routes. | Current route inventory below; `test_main_health_and_hosts::test_invalid_host_is_rejected_before_request_processing`; `test_nginx_runtime_security.py`; `test_ratelimit_policies.py`. | Resolved | Public auth and provider-authenticated callback endpoints must remain explicitly allow-listed. |
| P01.10 | Role-based access control was missing. | The permission catalogue, role presets, per-user grants/denials and server-side permission dependencies are implemented; vendor and dealer principals also have route allow-lists. | `test_users_permissions::test_roles_are_unioned`; `test_role_presets_reach_roles::test_every_permission_belongs_to_a_named_group`; `test_frontend_permission_gates.py`; `test_vendor_portal_rules.py`; `test_dealer_portal_rules.py`. | Resolved | Existing client-owned roles need administrator review for new grants; see the grant matrix in `docs/deployment.md`. |
| P01.11 | API responses were inconsistent. | Shared success, paginated and error envelopes are used by the API contract. | `test_response_contract::test_every_module_documents_consistent_json_responses`; `test_api_response_contract::test_handlers_share_the_error_contract`. | Resolved | None identified in the contract checks. |
| P01.12 | Errors could expose internals or produce inconsistent client responses. | Typed application errors and centralized handlers normalize validation, database and unexpected failures without returning SQL or stack traces. | `test_exception_handlers::test_the_client_never_sees_the_driver_message`; `test_api_response_contract::test_debug_logging_never_exposes_exception_text_to_clients`. | Resolved | Unexpected failures still require operational logging and alert ownership. |
| P01.13 | Critical actions lacked durable audit records. | Services record audit/status changes in the business transaction; the audit table is append-only and exposed through tenant-scoped queries. | `test_audit_query_service::test_the_audit_repository_refuses_to_mutate_the_log`; `test_audit_query_service::test_the_feed_is_tenant_scoped`; migration guard for `atcraft_reject_row_mutation`. | Resolved | Retention and off-box backup policy remain deployment decisions. |

### Current route and permission inventory

The checked-in OpenAPI contract contains **782 HTTP operations across 596
paths**. An AST scan of all router decorators, including router-level
`APIRouter.dependencies`, classified them as follows:

| Classification | Operations |
|---|---:|
| Permission-gated (`require_permission`, `require_any_permission` or `require_superuser`) | 763 |
| Authenticated self-service (`CurrentUser` or tenant context) | 13 |
| Public authentication endpoints | 5 |
| Provider-authenticated courier callback (HMAC or constant-time API key) | 1 |
| Unclassified | 0 |

The five public authentication routes are login, refresh, MFA verification,
password-reset request and password-reset confirmation. The six push routes
inherit `notification.view` from their router, rather than repeating the
dependency on each operation. This replaces the original assessment's
historical 494/480/9/5 counts; the API surface and the scan's handling of
inherited dependencies have both changed.

## Additional findings resolved during the programme

| Finding | Resolution and evidence |
|---|---|
| Vendor data was reachable outside the portal. | The principal allow-list blocks vendor principals from non-portal business routes; vendor records are scoped to the token-bound vendor. Regression coverage: `test_vendor_portal_rules.py` and `test_procurement_vendor_scope.py`. |
| Dealer data was reachable outside the portal. | The dealer principal is bound to its customer and limited to dealer-portal, notification and auth self-service routes. Regression coverage: `test_dealer_portal_rules.py`. |
| Vendor-invoice cancellation could leave financial effects unreversed. | Cancellation now follows the finance lifecycle and reverses the associated journal through the finance service. |
| Quality verdicts could strand owner documents at QC pending or move held stock twice (the T7 strand). | Quality owns the hold movement and calls the originating module in the same transaction. Regression coverage: `test_quality_owner_callbacks.py` and `test_quality_owners.py`. |
| Item attribute UUIDs could refer to another tenant or the wrong attribute kind. | `_require_attribute` performs tenant, kind and active-state checks on create/update. Regression coverage: `test_products_reference_probes.py`. |
| A1 brand/manufacturer assignment could race with retirement. | Assignment and lifecycle changes take conflicting row locks. Regression coverage: `test_brand_manufacturer_masters::test_an_assignment_and_a_lifecycle_change_take_conflicting_row_locks`. |
| Windows Smart App Control blocked unsigned pre-commit launchers. | `.pre-commit-config.yaml` invokes pre-commit hooks as modules through the signed Python interpreter instead of their generated executables. |
| Prior audit items were reported as dead or unwired. | Approval requests have procurement writers (`test_procurement_service.py`); routing `qc_required` is enforced by production (`test_quality_completion_e5.py`); `can_be_spare` is enforced by spare-reservation rules (`test_allocation_reservation_maths.py`). The unused `OrderPartyRepository.customer_price_type` method and import were removed; a source/test search finds no remaining callers. The unused balance-level `inventory_balances.allocated_quantity` was retired; it is distinct from the sales-order-line allocation field. |

## Legacy system review (P01.1)

The table maps each legacy module in S02.1-S02.15 to the current backend/API
and screen. “Partial” identifies a known functional gap; “planned” is not a
claim that the feature is already available.

| Legacy module | Replacement module and representative API | Current screen | Disposition |
|---|---|---|---|
| S02.1 Admin login and user management | `auth`, `users`; `/auth/*`, `/users`, `/roles` | `/login`, `/admin/users`, profile security | Present |
| S02.2 Dealer and customer management | `customers`, `dealer_portal`; `/customers`, `/dealer-portal/me` | `/customers`, `/dealers`, `/dealer/*` | Present; dealer-specific writes require dealer rights |
| S02.3 Product catalog management | `products`; `/products/items`, item variants, prices, images and accessories | `/items`, item detail | Partial: staff variant authoring, item price fields and item-vendor link editor remain absent |
| S02.4 Category, brand, manufacturer and specification masters | `products`; `/products/categories`, brand/manufacturer and item specification routes | `/masters`, item Specifications panel | Present |
| S02.5 Product variants | `products`, dealer catalogue; item variant routes and `/dealer-portal/catalog/*` | Dealer catalogue; no staff variant editor | Partial: dealer reads exist; staff authoring remains absent |
| S02.6 Dealer pricing and discount management | `products`, `dealer_portal`; `/promotions`, `/dealer-portal/offers`, dealer item-price reads | `/sales/promotions`, dealer catalogue | Partial: manual discount authority and item price editing remain incomplete |
| S02.7 Order management | `sales`; `/sales/*` | Sales orders and dealer orders | Present |
| S02.8 Goods return management | `sales_returns`, finance; `/sales-returns`, `/dealer-portal/returns`, customer adjustments | Sales returns, dealer Returns and customer adjustments | Present |
| S02.9 Invoice PDF generation | `sales`, finance, documents; dispatch invoice/PDF and dealer invoice routes | Dispatch, finance and dealer Invoices | Present |
| S02.10 Payment and ledger management | `finance`, `collections`; `/finance/*`, `/collections/*` | Finance, collections and receivables dashboard | Present |
| S02.11 Courier and transport management | `courier_tracking`, dispatch; provider tracking and webhook routes | Dispatch and transport reports | Present; external courier account/configuration is deployment-owned |
| S02.12 Banner and offer management | `dealer_portal`, promotions; `/promotions`, dealer banners/offers | `/sales/promotions`, dealer portal home/catalogue | Present |
| S02.13 Notifications | `notifications`, smart alerts and portal event writers; `/notifications/*` | Notification centre and Smart alerts administration | Present; live SMS/WhatsApp requires approved provider setup |
| S02.14 Reports and dashboards | `reports`, finance, procurement and operations | Dashboard and `/reports` | Present. Catalogue exports run as XLSX or PDF through `POST /reports/export` (`report.view` plus `report.export`); results above 5,000 rows switch to the same background path, and the 200,000-row export ceiling is stated in the artefact. Cost and value columns are gated by `item.cost_view`, `inventory_valuation.view` and their report-level permissions. |
| S02.15 Mobile API | `dealer_portal`; `/dealer-portal/*` | Mobile-first dealer PWA at `/dealer/*` | Present. The separate optional public catalogue is not shipped in this revision. |

### Superseded S02.14 trace notes

The original read-only trace in `docs/implementation/trace/notes.md` records
that small report results could not be exported, PDF exports were absent, and
`PRODUCT_PROFITABILITY` exposed cost without a cost permission. Those statements
describe the earlier audited baseline, not the current implementation; the
trace is retained unchanged as historical evidence. The current report catalogue
queues XLSX or PDF exports through `POST /reports/export` independently of the
5,000-row inline threshold, and the report-level permission is checked before
queuing. Cost reports require their owning cost permission, valuation fields
are removed from catalogue descriptors and export snapshots for callers without
valuation permission, and HTTP/service regressions cover these boundaries:
`tests/api/test_reports_permissions_http.py`,
`tests/unit/test_reports_catalogue.py` and
`tests/api/test_report_jobs_for_documents.py`. This closes the cited residual;
it does not assert parity with the unavailable legacy application.

The original legacy application source is unavailable in this repository.
Accordingly, mappings identify the delivered replacement and known remaining
scope; they do not certify parity with unprovided legacy behavior.

## Audit results and residual risks

- **Python production lock:** `pip-audit -r requirements.lock --no-deps
  --disable-pip --format columns` — no known vulnerabilities found.
- **Python development lock:** `pip-audit -r requirements-dev.lock --no-deps
  --disable-pip --format columns` — no known vulnerabilities found. These
  lockfile scans avoid installing Linux-only pins on Windows.
- **Frontend production dependencies:** `npm audit --omit=dev` — 0 findings.
- **Full frontend dependency tree:** `npm audit` — 7 moderate findings in the
  development-only `minimatch` / `typescript-eslint` dependency chain. The
  compatible lockfile update to `typescript-eslint` 8.72.1 did not clear the
  advisory. `npm audit fix --force` proposes a breaking parser downgrade; it
  was not applied. Revisit when an in-range upstream fix is available or after
  a separately reviewed toolchain migration.
- **Malware scanning:** configurable and implemented in
  `src/core/malware_scan.py`. `MALWARE_SCANNER=clamav` streams each stored
  upload to ClamAV (`zINSTREAM`) before the metadata row commits; a signature
  is refused (`400 FILE_INFECTED`) and the file deleted, and an unreachable
  daemon is refused (`503 FILE_SCAN_UNAVAILABLE`) under the default
  `MALWARE_SCAN_FAILURE_POLICY=closed`. Local development defaults to
  `MALWARE_SCANNER=none`; the deployed overlay defaults to `clamav`, starts a
  digest-pinned ClamAV service on the private Compose network, persists its
  signature database and waits for the image healthcheck. The deployment
  wrapper enables that service profile. An explicit `MALWARE_SCANNER=none`
  override still emits `app.malware_scanning_disabled`. Tests cover the
  protocol against a fake daemon, reply parsing, both failure policies,
  file/row cleanup and production Compose wiring. Signature freshness and
  capacity remain deployment monitoring responsibilities.
- **MFA enforcement:** optional per role. `MFA_REQUIRED_ROLES` names the role
  codes that must enrol TOTP before using the API; the shared authentication
  dependency blocks them with `403 MFA_REQUIRED` everywhere except enrolment,
  `/auth/me`, session management, password change and sign-out. Empty (the
  default) leaves MFA optional, so no existing account is locked out by an
  upgrade. The client decides which roles to list; that is a policy choice, not
  a code change.
- **DSC/PAdES signatures:** not implemented. Certificate procurement,
  signing authority, provider integration and retention are client decisions.
- **Public catalogue:** deliberately not built. The optional C3b/C4b public
  catalogue is not present in this revision: there is no `/public/catalog` API
  or `PUBLIC_CATALOG_COMPANY_CODE` setting. It remains out of scope unless the
  client asks for it.
- **Capacity run:** an indicative read-only run was made against a throwaway,
  fully migrated database on the development workstation (two 3-minute runs,
  0.30% and 0% failures, median 54-60 ms, p95 710-1 300 ms, 1.5-1.8 req/s);
  see `docs/performance.md`. That is a trend from a shared host with one API
  worker, not capacity evidence. Production sign-off still requires the
  client-agreed target, workload mix, thresholds and one-use write fixtures.

Vendor and dealer isolation, listed above, are resolved in code and are not
open items. Production commissioning, real provider accounts and
representative capacity evidence remain release gates. MFA remains optional
until the client names required roles; ClamAV signature freshness must be
monitored operationally.

## PDF build note

This report uses standard Markdown headings, lists and tables so it can be
reviewed alongside the handbook. From the repository root, build the handbook
PDF with `node tests/e2e/scripts/build-handbook.mjs`; it writes
`docs/handbook/Atcraft-ERP-Handbook.pdf`. The script builds the handbook, not
this standalone report; print this report separately when preparing a handover.
Inspect generated PDFs before distribution.

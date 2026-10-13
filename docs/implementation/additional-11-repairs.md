# Additional 11 repairs

Started 14 October 2026 (Asia/Calcutta), branch `fix/additional-11-20261014`.
Baseline: main `142f04d`. Existing main notes and all five development lanes are preserved.

Repairs are applied and verified in audit order. No production deployment or business database reset is authorized or performed. Tests use `atcraft_erp_test_additional11`.

| Issue | Repair | State |
|---|---|---|
| A01 | Credential authority on administrator-managed recovery email changes; session/reset grant invalidation | Fixed; 68 backend and 35 frontend tests passed; independent review passed |
| A02 | Warehouse scope on transfer/adjustment/count reads and actions | Pending |
| A03 | Warehouse scope on procurement/production destination and posting checks | Pending |
| A04 | Duplicate SO line dispatch validation | Pending |
| A05 | Carton content reconciliation, preserving mixed-item cartons | Pending |
| A06 | Company-currency customer credit/receipt safeguards | Pending |
| A07 | Export job/file access consistent with originating authorization | Pending |
| A08 | Recoverable packing PDF polling errors | Pending |
| A09 | Server search/pagination for batch selection | Pending |
| A10 | Reject unsupported noncash ADJUSTMENT before posting | Pending |
| A11 | Explicit-null validation returns normal validation errors | Pending |

No schema migration or dependency addition is planned. API contracts are regenerated after request/response changes. Financial restrictions preserve foreign-currency vendor payments and currency-separated reports.

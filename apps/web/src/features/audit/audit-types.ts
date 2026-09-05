/**
 * Shapes and helpers for the audit trail.
 *
 * `audit_logs` is an append-only ledger keyed on `(company_id, created_at, id)`,
 * so both the activity feed and an entity's history are read with keyset
 * cursors rather than page numbers - `OFFSET 40000` would make PostgreSQL walk
 * and discard forty thousand rows to answer a question about last Tuesday.
 */

import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api-client'
import { staleTimes } from '@/lib/query-client'
import type { CursorEnvelope } from '@/types/api'

/** The action vocabulary recorded by `src.modules.audit.models.AuditAction`. */
export const AUDIT_ACTIONS = [
  'LOGIN',
  'LOGIN_FAILED',
  'LOGOUT',
  'PASSWORD_CHANGED',
  'PASSWORD_RESET_REQUESTED',
  'CREATE',
  'UPDATE',
  'DELETE',
  'APPROVE',
  'REJECT',
  'CANCEL',
  'STATUS_CHANGE',
  'PERMISSION_CHANGE',
  'ROLE_CHANGE',
  'CONFIG_CHANGE',
  'DOCUMENT_UPLOAD',
  'DOCUMENT_DELETE',
  'DATA_EXPORT',
  'INVENTORY_MOVEMENT',
  'RESERVATION_CHANGE',
  'ALLOCATION_CHANGE',
  'REALLOCATION',
  'STOCK_ADJUSTMENT',
  'QC_DECISION',
  'PURCHASE_APPROVAL',
  'PRODUCTION_STATUS_CHANGE',
  'DISPATCH_CHANGE',
  'PAYMENT',
  'MRP_RUN',
  'JOB_STARTED',
  'JOB_FAILED',
] as const

export type AuditActionValue = (typeof AUDIT_ACTIONS)[number]

/**
 * Entity types the log records, in the order an investigator scans them.
 *
 * The server accepts any string here; this list exists so the filter is a
 * picker rather than a free-text field nobody can spell correctly.
 */
export const AUDIT_ENTITY_TYPES = [
  'user',
  'role',
  'company',
  'branch',
  'plant',
  'department',
  'financial_year',
  'item',
  'item_price',
  'warehouse',
  'storage_location',
  'vendor',
  'customer',
  'bom',
  'routing',
  'sales_order',
  'mrp_run',
  'purchase_requisition',
  'purchase_order',
  'goods_receipt',
  'purchase_return',
  'production_order',
  'material_issue',
  'production_entry',
  'assembly_order',
  'job_work_order',
  'qc_inspection',
  'packing_order',
  'dispatch',
  'vendor_invoice',
  'payment',
  'inventory_transaction',
  'stock_transfer',
  'stock_adjustment',
  'physical_count',
  'stock_reservation',
  'reallocation_request',
  'document',
] as const

export type AuditEntityType = (typeof AUDIT_ENTITY_TYPES)[number]

/**
 * One row of `GET /audit/logs` and `GET /audit/entity/...` (`AuditLogSummary`).
 *
 * Deliberately lean: the list carries no before/after values, no context and no
 * client fingerprint. Those are on `AuditLogDetail` and are fetched for the one
 * row an investigator expands, rather than shipped for every row of every page
 * of an append-only table.
 */
export interface AuditLogRow {
  id: string
  action: string
  entity_type: string
  entity_id: string | null
  entity_code: string | null
  summary: string
  changed_fields: string[] | null
  request_id: string | null
  actor_label: string | null
  created_by_id: string | null
  created_at: string
}

/** `GET /audit/logs/{id}` (`AuditLogRead`) - the row plus what actually changed. */
export interface AuditLogDetail extends AuditLogRow {
  old_values: Record<string, unknown> | null
  new_values: Record<string, unknown> | null
  context: Record<string, unknown> | null
  ip_address: string | null
  user_agent: string | null
}

/** Enough of `GET /users` to fill the actor filter. */
export interface AuditActorOption {
  id: string
  full_name: string
  email: string
}

export type BadgeTone = 'default' | 'secondary' | 'success' | 'warning' | 'destructive' | 'muted'

/**
 * Visual weight per action.
 *
 * Deliberately coarse: an investigator scanning a page of history needs to see
 * "something was destroyed or refused" at a glance, not thirty distinct hues.
 */
export function actionTone(action: string): BadgeTone {
  switch (action) {
    case 'CREATE':
    case 'APPROVE':
    case 'PURCHASE_APPROVAL':
      return 'success'
    case 'DELETE':
    case 'REJECT':
    case 'CANCEL':
    case 'LOGIN_FAILED':
    case 'JOB_FAILED':
    case 'DOCUMENT_DELETE':
      return 'destructive'
    case 'PERMISSION_CHANGE':
    case 'ROLE_CHANGE':
    case 'CONFIG_CHANGE':
    case 'DATA_EXPORT':
    case 'REALLOCATION':
    case 'STOCK_ADJUSTMENT':
      return 'warning'
    case 'LOGIN':
    case 'LOGOUT':
    case 'PASSWORD_CHANGED':
    case 'PASSWORD_RESET_REQUESTED':
      return 'muted'
    case 'UPDATE':
    case 'STATUS_CHANGE':
      return 'default'
    default:
      return 'secondary'
  }
}

/**
 * Where an audited record lives in this application.
 *
 * `entity_id` is a bare UUID, not a foreign key - the audit row outlives what
 * it describes - so a link is offered only where that id genuinely addresses a
 * screen. Everything else keeps its code as plain text rather than becoming a
 * link into nothing.
 */
const ENTITY_ROUTES: Record<string, (id: string) => string> = {
  item: (id) => `/items/${id}`,
  bom: (id) => `/bom/${id}`,
  sales_order: (id) => `/sales-orders/${id}`,
  purchase_order: (id) => `/procurement/purchase-orders/${id}`,
  production_order: (id) => `/production/${id}`,
  mrp_run: (id) => `/mrp/runs/${id}`,
  warehouse: () => '/warehouses',
  vendor: () => '/vendors',
  customer: () => '/customers',
  user: () => '/admin/users',
  role: () => '/admin/users?tab=roles',
  company: () => '/admin/organisation',
  branch: () => '/admin/organisation?tab=branches',
  plant: () => '/admin/organisation?tab=plants',
  department: () => '/admin/organisation?tab=departments',
  financial_year: () => '/admin/organisation?tab=financial-years',
  purchase_requisition: () => '/procurement/requisitions',
  goods_receipt: () => '/procurement/goods-receipts',
  vendor_invoice: () => '/finance/vendor-invoices',
  payment: () => '/finance',
  qc_inspection: () => '/quality',
  dispatch: () => '/dispatch',
  packing_order: () => '/packing',
  assembly_order: () => '/assembly',
  job_work_order: () => '/job-work',
  routing: () => '/routing',
  stock_adjustment: () => '/inventory/movements',
  stock_transfer: () => '/inventory/movements',
  physical_count: () => '/inventory/movements',
  stock_reservation: () => '/inventory/reservations',
  reallocation_request: () => '/inventory/reservations',
  inventory_transaction: () => '/inventory/ledger',
}

export function entityLink(entityType: string, entityId: string | null): string | null {
  const build = ENTITY_ROUTES[entityType]
  return build && entityId ? build(entityId) : null
}

// ------------------------------------------------------------------ diff ----

export interface FieldChange {
  field: string
  before: unknown
  after: unknown
}

/**
 * The fields that actually changed, in a stable order.
 *
 * The API already stores only changed keys, but `changed_fields` can be null on
 * rows written before a diff was computed (a login, a status change), so the
 * union of both value maps is the fallback.
 */
export function changedFields(row: AuditLogDetail): FieldChange[] {
  const before = row.old_values ?? {}
  const after = row.new_values ?? {}
  const names =
    row.changed_fields && row.changed_fields.length > 0
      ? row.changed_fields
      : [...new Set([...Object.keys(before), ...Object.keys(after)])].sort()

  return names.map((field) => ({ field, before: before[field], after: after[field] }))
}

/** Renders a JSONB value as one readable line. */
export function formatAuditValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value === '' ? '(empty)' : value
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (typeof value === 'number') return String(value)
  if (Array.isArray(value))
    return value.length === 0 ? '(none)' : value.map(formatAuditValue).join(', ')
  return JSON.stringify(value)
}

// ----------------------------------------------------------------- data -----

/**
 * `api.getPage` returns the envelope untouched; only its TypeScript shape
 * assumes offset pagination. The audit envelope is cursor-shaped, so it is
 * re-typed here rather than widening the shared client for two endpoints.
 */
export async function fetchAuditPage(
  path: string,
  params: Record<string, unknown>,
): Promise<CursorEnvelope<AuditLogRow>> {
  const envelope = await api.getPage<AuditLogRow>(path, { params })
  return envelope as unknown as CursorEnvelope<AuditLogRow>
}

/**
 * The full entry behind one list row.
 *
 * One request, made when a row is expanded, for that row only. The list
 * endpoint stopped carrying the value maps, and re-requesting them per visible
 * row would be an N+1 against the largest table in the system.
 */
export function useAuditLogDetail(logId: string | null) {
  return useQuery({
    queryKey: ['audit', 'logs', 'detail', logId] as const,
    queryFn: () => api.get<AuditLogDetail>(`/audit/logs/${logId}`),
    // Audit rows are immutable once written.
    staleTime: staleTimes.reference,
    enabled: Boolean(logId),
  })
}

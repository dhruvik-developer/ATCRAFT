/**
 * Notification shapes.
 *
 * `notifications` is append-heavy and grows without bound, so the list is read
 * with a keyset cursor rather than page numbers (CLAUDE.md, pagination rule).
 *
 * Everything here mirrors `NotificationRead` / `NotificationPreferenceRead` on
 * the API. Two things about that contract shape the screens above it: the row
 * carries its own `action_url` rather than a document code for the UI to route
 * from, and repeated events are collapsed into one row with an
 * `occurrence_count` instead of arriving as N rows.
 */

import { api } from '@/lib/api-client'
import type { CursorEnvelope } from '@/types/api'

export const SEVERITIES = ['INFO', 'SUCCESS', 'WARNING', 'CRITICAL'] as const
export type Severity = (typeof SEVERITIES)[number]

/**
 * The event vocabulary, from `NotificationType`.
 *
 * Listed so the type filter is a picker rather than a free-text box, and so the
 * preferences table can be rendered in a deliberate order rather than whatever
 * order the API happens to return.
 */
export const NOTIFICATION_TYPES = [
  'APPROVAL_PENDING',
  'APPROVAL_GRANTED',
  'APPROVAL_REJECTED',
  'PURCHASE_ORDER_DELAYED',
  'PRODUCTION_ORDER_DELAYED',
  'JOB_WORK_OVERDUE',
  'DISPATCH_DELAYED',
  'SALES_ORDER_AT_RISK',
  'STOCK_BELOW_REORDER',
  'STOCK_SHORTAGE',
  'ALLOCATION_CHANGED',
  'QC_INSPECTION_PENDING',
  'QC_FAILED',
  'GOODS_RECEIPT_POSTED',
  'PAYMENT_DUE',
  'MRP_RUN_COMPLETED',
  'REPORT_READY',
  'BACKGROUND_JOB_COMPLETED',
  'BACKGROUND_JOB_FAILED',
  'PURCHASE_ORDER_SENT',
  'PURCHASE_ORDER_ACKNOWLEDGED',
  'PURCHASE_ORDER_DECLINED',
  'PURCHASE_ORDER_DATE_CHANGED',
  'PURCHASE_ORDER_READY',
  'PURCHASE_ORDER_SHIPPED',
  'VENDOR_INVOICE_STATUS',
  'VENDOR_PAYMENT_RECORDED',
  'SYSTEM_ANNOUNCEMENT',
] as const
export type NotificationType = (typeof NOTIFICATION_TYPES)[number]

/** One row of `GET /notifications`. */
export interface NotificationRow {
  id: string
  /** Event key, e.g. `APPROVAL_PENDING`. Also the preference key. */
  notification_type: string
  severity: string
  title: string
  body: string
  /**
   * Where this notification points, decided by the API. The row carries no
   * document code, so this is the only route into the record behind it.
   */
  action_url: string | null
  entity_type: string | null
  entity_id: string | null
  /** Repeats collapsed into this row; 1 means it happened once. */
  occurrence_count: number
  /** Set when several events were folded together under one key. */
  group_key: string | null
  /** Delivery channels this one actually went out on. */
  channels: string[]
  delivered_at: string | null
  is_read: boolean
  read_at: string | null
  created_at: string
  updated_at: string
}

/** One row of `GET /notifications/preferences`. */
export interface NotificationPreference {
  notification_type: string
  in_app_enabled: boolean
  email_enabled: boolean
  push_enabled?: boolean
  /** False when this row is still the system default rather than a choice. */
  is_customised?: boolean
}

/** `GET /notifications/unread-count`. */
export interface UnreadCount {
  unread_count: number
  /** True when the count hit its ceiling and is a floor, not an exact figure. */
  is_capped?: boolean
}

/** `POST /notifications/read-all`. */
export interface MarkAllReadResult {
  marked: number
  remaining_unread: number
}

/**
 * `api.getPage` returns the envelope untouched; only its TypeScript shape
 * assumes offset pagination. This endpoint's envelope is cursor-shaped.
 */
export async function fetchNotificationPage(
  params: Record<string, unknown>,
): Promise<CursorEnvelope<NotificationRow>> {
  const envelope = await api.getPage<NotificationRow>('/notifications', { params })
  return envelope as unknown as CursorEnvelope<NotificationRow>
}

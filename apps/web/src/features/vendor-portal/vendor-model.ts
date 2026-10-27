/** Portal response types are allow-lists owned by the API, never staff projections. */
import { orderedNetUnitPrice } from '@/features/finance/finance-types'
import { toDecimal } from '@/lib/format'
import type { SchemaOf } from '@/types/api'

export type VendorProfile = SchemaOf<'VendorPortalMe'>
export type VendorDashboard = SchemaOf<'VendorPortalDashboard'>
export type VendorOrderRow = SchemaOf<'InboundTrackingRead'>
export type VendorOrder = SchemaOf<'VendorPortalOrderDetail'>
export type VendorPendingAmendment = SchemaOf<'VendorPortalPendingAmendmentRead'>
export type VendorOrderResponse = VendorOrder | VendorPendingAmendment
export type VendorShipment = SchemaOf<'InboundShipmentRead'>
export type VendorDocument = SchemaOf<'InboundDocumentRead'>
export type VendorReceipt = SchemaOf<'VendorPortalReceiptRead'>
export type VendorInvoice = SchemaOf<'VendorPortalInvoiceDetail'>
export type VendorInvoiceRow = SchemaOf<'VendorPortalInvoiceSummary'>
export type VendorPayment = SchemaOf<'VendorPortalPayment'>
export type VendorPaymentSummary = SchemaOf<'VendorPortalPaymentSummary'>
export type VendorRating = SchemaOf<'VendorRatingRead'>

export const orderTabs = [
  ['to_accept', 'To accept'],
  ['accepted', 'Accepted'],
  ['ready', 'Ready'],
  ['dispatched', 'Dispatched'],
  ['delivered', 'Delivered'],
  ['all', 'All'],
] as const

export function orderTab(value: string | null): (typeof orderTabs)[number][0] {
  return orderTabs.find(([key]) => key === value)?.[0] ?? 'all'
}

/** These are the two accepted states allowed by the shared inbound service. */
export function canShipOrder(order: Pick<VendorOrder, 'status'>): boolean {
  return order.status === 'ACKNOWLEDGED' || order.status === 'PARTIALLY_RECEIVED'
}

/** Copy accepted receipt quantities and the agreed net price, keeping decimal strings. */
export function receiptInvoiceLines(order: VendorOrder, receipt: VendorReceipt) {
  return receipt.lines
    .filter((line) => toDecimal(line.accepted_quantity).greaterThan(0))
    .map((line) => {
      const ordered = order.lines.find((candidate) => candidate.id === line.purchase_order_line_id)
      return {
        item_id: line.item_id,
        purchase_order_line_id: line.purchase_order_line_id,
        goods_receipt_line_id: line.id,
        description: ordered?.item_name ?? ordered?.description ?? '',
        hsn_code: '',
        quantity: line.accepted_quantity,
        uom_id: ordered?.uom_id ?? null,
        unit_price: ordered ? (orderedNetUnitPrice(ordered) ?? '0') : '0',
        discount_amount: '0',
        tax_rate: ordered?.tax_rate ?? '0',
        is_interstate: false,
      }
    })
}

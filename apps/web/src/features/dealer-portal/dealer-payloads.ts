import { toDateInput, toDecimal } from '@/lib/format'
import type { SchemaOf } from '@/types/api'

export function isIsoCalendarDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000')) return false
  const parsed = new Date(`${value}T00:00:00Z`)
  return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value
}

export function dealerQuantity(value: string): string {
  if (!/^\d{1,14}(\.\d{1,4})?$/.test(value) || !toDecimal(value).greaterThan(0)) {
    throw new Error('Enter a positive quantity with up to 4 decimal places.')
  }
  return value
}

/** Prices, offers and customer identity are always derived by the server. */
export function dealerCheckoutPayload(
  cartVersion: number | null,
  fields: FormData,
  today = toDateInput(new Date()),
): SchemaOf<'DealerCheckoutRequest'> {
  if (!cartVersion) throw new Error('Refresh the cart before placing the order.')
  const requiredDate = String(fields.get('required_date') ?? '')
  if (!isIsoCalendarDate(requiredDate) || requiredDate < today)
    throw new Error('Choose a required date today or later.')
  const optional = (name: string) => String(fields.get(name) ?? '').trim() || null
  return {
    cart_version: cartVersion,
    required_date: requiredDate,
    shipping_address_id: optional('shipping_address_id'),
    billing_address_id: optional('billing_address_id'),
    customer_po_number: optional('customer_po_number'),
    notes: optional('notes'),
  }
}

export function dealerReturnPayload(
  dispatch: SchemaOf<'DealerEligibleDispatch'>,
  fields: FormData,
): SchemaOf<'DealerReturnCreate'> {
  const lines = dispatch.lines.flatMap((line) => {
    const raw = String(fields.get(`quantity-${line.dispatch_line_id}`) ?? '').trim()
    if (!raw) return []
    const quantity = dealerQuantity(raw)
    if (toDecimal(quantity).greaterThan(line.returnable_quantity))
      throw new Error(
        `The return quantity for ${line.item_code} exceeds the remaining eligible quantity.`,
      )
    const reason = String(fields.get(`reason-${line.dispatch_line_id}`) ?? '').trim()
    if (reason.length < 2 || reason.length > 32)
      throw new Error('Enter a reason between 2 and 32 characters for each return line.')
    return [
      {
        dispatch_line_id: line.dispatch_line_id,
        quantity,
        reason_code: reason,
        note: String(fields.get(`note-${line.dispatch_line_id}`) ?? '').trim() || null,
      },
    ]
  })
  if (!lines.length) throw new Error('Enter a return quantity for at least one line.')
  return {
    dispatch_id: dispatch.id,
    lines,
    notes: String(fields.get('notes') ?? '').trim() || null,
  }
}

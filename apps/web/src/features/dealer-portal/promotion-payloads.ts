import { toDecimal } from '@/lib/format'
import type { SchemaOf } from '@/types/api'

import { isIsoCalendarDate } from './dealer-payloads'

function member<const T extends readonly string[]>(value: string, choices: T): T[number] {
  if (!choices.includes(value)) throw new Error('Choose a valid promotion option.')
  return value
}

function promotionFields(fields: FormData) {
  const text = (key: string) => String(fields.get(key) ?? '').trim()
  const code = text('code')
  const title = text('title')
  if (!/^[A-Za-z0-9][A-Za-z0-9_/-]{0,31}$/.test(code))
    throw new Error('Enter a code with up to 32 letters, digits, slashes, underscores or hyphens.')
  if (!title || title.length > 160) throw new Error('Enter a title with up to 160 characters.')
  const validFrom = text('valid_from') || null
  const validTo = text('valid_to') || null
  if ([validFrom, validTo].some((value) => value && !isIsoCalendarDate(value)))
    throw new Error('Enter a valid promotion date.')
  if (validFrom && validTo && validTo < validFrom)
    throw new Error('The validity end date must be on or after the start date.')
  const sort = Number(text('sort_order'))
  if (!Number.isSafeInteger(sort) || sort < 0 || sort > 100000)
    throw new Error('Sort order must be a whole number between 0 and 100000.')
  const audience = text('audience_price_type')
  return {
    code,
    title,
    valid_from: validFrom,
    valid_to: validTo,
    sort_order: sort,
    audience_price_type: audience
      ? member(audience, ['SALE', 'DEALER', 'DISTRIBUTOR'] as const)
      : null,
  }
}

export function bannerPayload(
  fields: FormData,
  imageId: string,
  targetId: string | null,
): SchemaOf<'DealerBannerCreate'> {
  if (!imageId) throw new Error('Upload a banner image.')
  const kind = member(String(fields.get('link_kind') ?? 'NONE'), [
    'NONE',
    'ITEM',
    'CATEGORY',
    'BRAND',
    'OFFER',
  ] as const)
  if (kind !== 'NONE' && !targetId) throw new Error('Choose the banner link target.')
  return {
    ...promotionFields(fields),
    subtitle: String(fields.get('subtitle') ?? '').trim() || null,
    image_document_id: imageId,
    link_kind: kind,
    link_id: kind === 'NONE' ? null : targetId,
    status: 'INACTIVE',
  }
}

export function offerPayload(
  fields: FormData,
  targetId: string | null,
  customerId: string | null,
): SchemaOf<'DealerOfferCreate'> {
  const kind = member(String(fields.get('scope_kind') ?? 'ALL'), [
    'ALL',
    'ITEM',
    'CATEGORY',
    'BRAND',
  ] as const)
  if (kind !== 'ALL' && !targetId) throw new Error('Choose the offer scope target.')
  const minQuantity = String(fields.get('min_quantity') ?? '').trim()
  const discount = String(fields.get('discount_percentage') ?? '').trim()
  if (!/^\d{1,14}(\.\d{1,4})?$/.test(minQuantity) || !toDecimal(minQuantity).greaterThan(0))
    throw new Error('Enter a positive minimum quantity with up to 4 decimal places.')
  if (
    !/^\d{1,3}(\.\d{1,4})?$/.test(discount) ||
    !toDecimal(discount).greaterThan(0) ||
    toDecimal(discount).greaterThan(100)
  )
    throw new Error('Discount must be greater than 0 and at most 100%.')
  return {
    ...promotionFields(fields),
    description: String(fields.get('description') ?? '').trim() || null,
    scope_kind: kind,
    category_id: kind === 'CATEGORY' ? targetId : null,
    brand_id: kind === 'BRAND' ? targetId : null,
    item_id: kind === 'ITEM' ? targetId : null,
    customer_id: customerId,
    min_quantity: minQuantity,
    discount_percentage: discount,
    is_active: false,
  }
}

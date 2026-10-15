/**
 * The item-vendor link add/edit dialog.
 *
 * What matters here is the same pair as the variant dialog: **the request
 * bodies** - creation names the vendor, an edit never repoints one (identity
 * is changed by removing and adding, not by rewriting a row other documents
 * reference), and neither direction ever writes the last purchase price - and
 * **the gates**, which live on `item.update` for all four writes.
 *
 * One Radix `<Select>` is opened below (the vendor picker on a create); edits
 * keep the seeded value without opening anything.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen, type RouteTable } from '@/test/harness'

import type { ItemVendorLink } from './item-model'
import { VendorLinkDialog } from './vendor-link-dialog'

const ITEM_ID = '01a00000-0000-7000-8000-0000000000i1'

const VENDORS = ok([
  {
    id: 'vendor-fast',
    code: 'FASTNERS',
    name: 'Fastners & Co',
    vendor_types: ['RAW_MATERIAL_SUPPLIER'],
    is_approved: true,
    rating: null,
    lead_time_days: 7,
  },
])

const MAKERS = page([
  {
    id: 'maker-cyl',
    code: 'CYLINDER',
    name: 'Demo Cylinder Works',
    legal_name: null,
    country: 'India',
    website: null,
    notes: null,
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
  },
])

const LINK: ItemVendorLink = {
  id: 'link-1',
  item_id: ITEM_ID,
  vendor_id: 'vendor-fast',
  vendor_item_code: 'PF-CYL-60-5P',
  vendor_item_name: 'Pin cylinder 60mm',
  manufacturer_id: 'maker-cyl',
  manufacturer_part_number: 'SCW-PC60-5P',
  is_preferred: true,
  lead_time_days: 15,
  min_order_quantity: '10.0000',
  order_multiple: '5.0000',
  last_purchase_price: '845.5000',
  last_purchase_date: '2026-09-14',
  currency: 'INR',
  is_active: true,
}

function renderDialog(options: {
  permissions: string[]
  routes: RouteTable
  link?: ItemVendorLink | null
}) {
  return renderScreen(
    <VendorLinkDialog
      open
      onOpenChange={() => {}}
      itemId={ITEM_ID}
      itemCode="FG-CYL-60"
      link={options.link ?? null}
    />,
    { permissions: options.permissions, routes: options.routes },
  )
}

describe('linking a vendor', () => {
  it('posts the vendor, the commercial terms and a null maker when none is picked', async () => {
    const { user, fetch } = renderDialog({
      permissions: [P.ITEM_UPDATE, P.VENDOR_VIEW],
      routes: {
        'GET /vendors/options': VENDORS,
        'GET /products/manufacturers': MAKERS,
        [`POST /products/items/${ITEM_ID}/vendors`]: ok(LINK),
      },
    })

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('combobox', { name: 'Vendor' }))
    await user.click(await screen.findByRole('option', { name: /Fastners/ }))
    await user.type(within(dialog).getByLabelText('Vendor item code'), 'PF-CYL-60-5P')
    await user.type(within(dialog).getByLabelText('Vendor item name'), 'Pin cylinder 60mm')
    const lead = within(dialog).getByRole('textbox', { name: 'Lead time (days)' })
    await user.clear(lead)
    await user.type(lead, '15')
    await user.click(within(dialog).getByRole('checkbox', { name: /preferred source/i }))
    await user.click(within(dialog).getByRole('button', { name: /link vendor/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('POST', `/products/items/${ITEM_ID}/vendors`),
    )) as Record<string, unknown>
    expect(body).toEqual({
      vendor_id: 'vendor-fast',
      vendor_item_code: 'PF-CYL-60-5P',
      vendor_item_name: 'Pin cylinder 60mm',
      manufacturer_id: null,
      manufacturer_part_number: null,
      is_preferred: true,
      lead_time_days: 15,
      min_order_quantity: '0',
      order_multiple: '0',
    })
  })

  it('requires a vendor before anything is sent', async () => {
    const { user, fetch } = renderDialog({
      permissions: [P.ITEM_UPDATE, P.VENDOR_VIEW],
      routes: {
        'GET /vendors/options': VENDORS,
        'GET /products/manufacturers': MAKERS,
      },
    })

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /link vendor/i }))

    expect(await within(dialog).findByText('Choose a vendor')).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })
})

describe('editing a link', () => {
  it('clears the vendor code and name with empty strings rather than the API no-op null', async () => {
    const { user, fetch } = renderDialog({
      permissions: [P.ITEM_UPDATE],
      routes: {
        'PATCH /products/item-vendors/link-1': ok({
          ...LINK,
          vendor_item_code: '',
          vendor_item_name: '',
        }),
      },
      link: LINK,
    })

    const dialog = await screen.findByRole('dialog')
    await user.clear(within(dialog).getByLabelText('Vendor item code'))
    await user.clear(within(dialog).getByLabelText('Vendor item name'))
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('PATCH', '/products/item-vendors/link-1'),
    )) as Record<string, unknown>
    expect(body.vendor_item_code).toBe('')
    expect(body.vendor_item_name).toBe('')
  })

  it('patches the terms without repointing the vendor, and shows the last price read-only', async () => {
    // The vendor is the row's identity: it is displayed, locked, and never
    // sent back. The last purchase price is written by goods receipts, so it
    // is shown as a sentence and appears in no payload.
    const { user, fetch } = renderDialog({
      permissions: [P.ITEM_UPDATE, P.VENDOR_VIEW, P.MANUFACTURER_VIEW],
      routes: {
        'GET /vendors/options': VENDORS,
        'GET /products/manufacturers': MAKERS,
        'PATCH /products/item-vendors/link-1': ok({ ...LINK, lead_time_days: 20 }),
      },
      link: LINK,
    })

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('combobox', { name: 'Vendor' })).toBeDisabled()
    expect(within(dialog).getByText(/never purchased|845\.50/)).toBeVisible()

    const lead = within(dialog).getByRole('textbox', { name: 'Lead time (days)' })
    await user.clear(lead)
    await user.type(lead, '20')
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('PATCH', '/products/item-vendors/link-1'),
    )) as Record<string, unknown>
    expect(body.vendor_id).toBeUndefined()
    expect(body.lead_time_days).toBe(20)
    expect(body.vendor_item_code).toBe('PF-CYL-60-5P')
    expect(body.manufacturer_id).toBe('maker-cyl')
    expect(body.manufacturer_part_number).toBe('SCW-PC60-5P')
    expect(body.is_preferred).toBe(true)
    expect(body.is_active).toBe(true)
    expect('last_purchase_price' in body).toBe(false)
  })
})

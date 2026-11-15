/**
 * The quality reports.
 *
 * The vendor record breaks a vendor's rejections down by item. The breakdown
 * carried item ids only, so the table and the chart printed the first eight
 * characters of each uuid; `VendorQualityItemRow` now names the item (G-02).
 */

import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, page, renderScreen } from '@/test/harness'

import { QualityReports } from './quality-reports'

const VENDOR = '01a00000-0000-7000-8000-0000000000v1'
const ITEM = '01a00000-0000-7000-8000-0000000000i1'

describe('the vendor quality record', () => {
  it('names each item it breaks the rejections down by, never its id', async () => {
    const { user } = renderScreen(<QualityReports />, {
      permissions: [P.QC_VIEW, P.VENDOR_VIEW, P.ITEM_VIEW],
      routes: {
        'GET /products/items': page([]),
        'GET /vendors/options': ok([
          {
            id: VENDOR,
            code: 'VEN-007',
            name: 'Acme Castings',
            vendor_types: ['SUPPLIER'],
            is_approved: true,
            rating: null,
            lead_time_days: 7,
          },
        ]),
        [`GET /quality/reports/vendor/${VENDOR}`]: ok({
          vendor_id: VENDOR,
          window_days: 180,
          window_start: '2026-05-20',
          window_end: '2026-11-16',
          has_evidence: true,
          inspection_count: 3,
          inspected_quantity: '300.0000',
          accepted_quantity: '270.0000',
          rejected_quantity: '30.0000',
          rework_quantity: '0.0000',
          scrap_quantity: '0.0000',
          rejection_rate: '10.00',
          items_truncated: false,
          items: [
            {
              item_id: ITEM,
              item_code: 'HNG-SS-100',
              item_name: 'Stainless hinge 100 mm',
              inspection_count: 3,
              inspected_quantity: '300.0000',
              rejected_quantity: '30.0000',
              scrap_quantity: '0.0000',
              rejection_rate: '10.00',
            },
          ],
        }),
      },
    })

    await user.click(await screen.findByRole('combobox', { name: 'Vendor' }))
    await user.click(await screen.findByRole('option', { name: 'VEN-007 - Acme Castings' }))

    const table = await screen.findByRole('table', {
      name: /rejection rate by item for the selected window/i,
    })
    expect(
      within(table).getByRole('link', { name: 'HNG-SS-100 - Stainless hinge 100 mm' }),
    ).toHaveAttribute('href', `/items/${ITEM}`)
    expect(within(table).queryByText(/01a00000/)).toBeNull()
  })
})

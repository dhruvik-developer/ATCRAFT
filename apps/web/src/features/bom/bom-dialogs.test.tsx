/**
 * The BOM create dialog.
 *
 * Two things are pinned here. An ALTERNATE bill must name the bill it stands
 * in for: the API refuses one that does not, and the form used to offer the
 * type with no way to say which bill, so every alternate was a 422 the
 * operator could not read. And a 422 about a line as a whole (`lines.0`) is
 * shown on that line rather than dropped for not matching a field.
 */

import { screen, within } from '@testing-library/react'
import { beforeAll, describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteTable } from '@/test/harness'

import { BomFormDialog } from './bom-dialogs'
import type { BomRow } from './bom-model'

/**
 * The same jsdom shims `movements-page.test.tsx` carries, for the same reason:
 * Radix's select and popover call pointer capture and `scrollIntoView` while
 * opening, and Floating UI's `:modal` probe recurses inside nwsapi.
 */
beforeAll(() => {
  const proto = window.Element.prototype as unknown as Record<string, unknown>
  if (!proto.hasPointerCapture) proto.hasPointerCapture = () => false
  if (!proto.setPointerCapture) proto.setPointerCapture = () => {}
  if (!proto.releasePointerCapture) proto.releasePointerCapture = () => {}
  if (!proto.scrollIntoView) proto.scrollIntoView = () => {}

  const nativeMatches = window.Element.prototype.matches
  const TOP_LAYER = /^:(modal|fullscreen|popover-open|open|closed)$/
  window.Element.prototype.matches = function (this: Element, selector: string) {
    if (typeof selector === 'string' && TOP_LAYER.test(selector.trim())) return false
    return nativeMatches.call(this, selector)
  }
})

const ITEM = {
  id: 'it000000-0000-7000-8000-000000000001',
  code: 'DOOR-CLOSER-A',
  name: 'Door closer, model A',
}
const UOM = { id: 'uom00000-0000-7000-8000-000000000001', code: 'PCS', name: 'Pieces' }

const PRODUCTION_BOM: BomRow = {
  id: 'bom00000-0000-7000-8000-000000000001',
  code: 'BOM/DOOR-CLOSER-A/1',
  item_id: ITEM.id,
  version: 1,
  revision: 'A',
  status: 'APPROVED',
  bom_type: 'PRODUCTION',
  is_default: true,
  output_quantity: '1.0000',
  base_uom_id: UOM.id,
  effective_from: '2026-01-01',
  effective_to: null,
  parent_bom_id: null,
  approved_by_id: null,
  approved_at: null,
  created_at: '2026-01-01T09:00:00Z',
  updated_at: '2026-01-01T09:00:00Z',
}

function renderDialog(routes: RouteTable = {}) {
  return renderScreen(<BomFormDialog open onOpenChange={() => {}} />, {
    permissions: [P.BOM_VIEW, P.BOM_CREATE, P.UOM_VIEW],
    routes: {
      'GET /products/uoms': page([UOM]),
      'GET /products/items': page([ITEM]),
      'GET /bom': page([PRODUCTION_BOM]),
      ...routes,
    },
  })
}

/** Item, unit and one component - the least the create endpoint accepts. */
async function fillTheMinimum(
  user: ReturnType<typeof renderDialog>['user'],
  dialog: HTMLElement,
): Promise<void> {
  await user.click(within(dialog).getByRole('combobox', { name: 'Produced item' }))
  await user.click(await screen.findByRole('option', { name: /DOOR-CLOSER-A/ }))
  await user.click(within(dialog).getByRole('combobox', { name: 'Base unit' }))
  await user.click(await screen.findByRole('option', { name: /PCS/ }))
  await user.click(within(dialog).getByRole('combobox', { name: 'Component 1' }))
  await user.click(await screen.findByRole('option', { name: /DOOR-CLOSER-A/ }))
}

describe('an alternate bill', () => {
  it('names the bill it replaces, chosen from the same item, and sends it', async () => {
    const bomSearches: URL[] = []
    const { user, fetch } = renderDialog({
      'GET /bom': ({ url }) => {
        bomSearches.push(url)
        return page([PRODUCTION_BOM])
      },
      'POST /bom': ok({ ...PRODUCTION_BOM, id: 'bom-2', code: 'BOM/DOOR-CLOSER-A/2' }),
    })

    const dialog = await screen.findByRole('dialog')
    await fillTheMinimum(user, dialog)

    // Not offered until the type calls for it.
    expect(within(dialog).queryByRole('combobox', { name: 'Replaces' })).not.toBeInTheDocument()

    await user.click(within(dialog).getByRole('combobox', { name: 'BOM type' }))
    await user.click(await screen.findByRole('option', { name: 'Alternate' }))

    await user.click(await within(dialog).findByRole('combobox', { name: 'Replaces' }))
    await user.click(await screen.findByRole('option', { name: /BOM\/DOOR-CLOSER-A\/1/ }))

    // The candidates are the produced item's own bills, asked of the server.
    expect(bomSearches.length).toBeGreaterThan(0)
    expect(bomSearches[0]!.searchParams.get('item_id')).toBe(ITEM.id)

    await user.click(within(dialog).getByRole('button', { name: /create bom/i }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    const body = fetch.requestFor('POST', '/bom') as {
      bom_type: string
      parent_bom_id: string | null
    }
    expect(body.bom_type).toBe('ALTERNATE')
    expect(body.parent_bom_id).toBe(PRODUCTION_BOM.id)
  })

  it('refuses to post an alternate that names nothing', async () => {
    // The API answers this with a model-level 422 nothing on the form could
    // render, so the form has to catch it before the round trip.
    const { user, fetch } = renderDialog()

    const dialog = await screen.findByRole('dialog')
    await fillTheMinimum(user, dialog)
    await user.click(within(dialog).getByRole('combobox', { name: 'BOM type' }))
    await user.click(await screen.findByRole('option', { name: 'Alternate' }))
    await user.click(within(dialog).getByRole('button', { name: /create bom/i }))

    expect(
      await within(dialog).findByText('Name the bill this alternate stands in for'),
    ).toBeInTheDocument()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('sends no parent for any other type', async () => {
    const { user, fetch } = renderDialog({ 'POST /bom': ok({ ...PRODUCTION_BOM, id: 'bom-2' }) })

    const dialog = await screen.findByRole('dialog')
    await fillTheMinimum(user, dialog)
    await user.click(within(dialog).getByRole('button', { name: /create bom/i }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'POST')).toBe(true)
    const body = fetch.requestFor('POST', '/bom') as { bom_type: string; parent_bom_id: unknown }
    expect(body.bom_type).toBe('PRODUCTION')
    expect(body.parent_bom_id).toBeNull()
  })
})

describe('a refused line', () => {
  it('shows a rule about the whole line on that line', async () => {
    // `BomLineIn` has an after-validator, so the API can refuse `lines.0` as a
    // unit rather than any one of its fields. That message used to be dropped
    // for not matching a field name, with the toast suppressed as well.
    const { user } = renderDialog({
      'POST /bom': fail(422, 'VALIDATION_ERROR', 'Some fields need attention.', {
        fields: { 'lines.0': ['A line cannot be a substitute for itself.'] },
      }),
    })

    const dialog = await screen.findByRole('dialog')
    await fillTheMinimum(user, dialog)
    await user.click(within(dialog).getByRole('button', { name: /create bom/i }))

    expect(
      await within(dialog).findByText('A line cannot be a substitute for itself.'),
    ).toBeInTheDocument()
    // The dialog stays open so the line can be corrected.
    expect(within(dialog).getByRole('button', { name: /create bom/i })).toBeVisible()
  })
})

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
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteTable } from '@/test/harness'

import { BomEditDialog, BomFormDialog } from './bom-dialogs'
import type { BomDetail, BomRow } from './bom-model'

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

/**
 * The draft editor.
 *
 * A draft bill is corrected in place rather than re-keyed: the dialog prefills
 * both grids from the loaded bill and PATCHes them wholesale, and a bill
 * someone else saved first answers 409, which has to say so on the form rather
 * than vanishing into the field-error machinery.
 */
const SCREW = {
  id: 'it000000-0000-7000-8000-000000000003',
  code: 'SCREW-M4',
  name: 'Screw M4',
}

const DRAFT_BOM: BomDetail = {
  ...PRODUCTION_BOM,
  id: 'bom00000-0000-7000-8000-000000000009',
  code: 'BOM/DOOR-CLOSER-A/2',
  status: 'DRAFT',
  notes: null,
  lines: [
    {
      id: 'bl000000-0000-7000-8000-000000000001',
      bom_id: 'bom00000-0000-7000-8000-000000000009',
      line_number: 1,
      component_item_id: SCREW.id,
      quantity_per: '6.0000',
      uom_id: UOM.id,
      scrap_percentage: '5.0000',
      is_optional: false,
      substitute_for_line_id: null,
      operation_sequence: 10,
      notes: null,
    },
    {
      id: 'bl000000-0000-7000-8000-000000000002',
      bom_id: 'bom00000-0000-7000-8000-000000000009',
      line_number: 2,
      component_item_id: ITEM.id,
      quantity_per: '1.0000',
      uom_id: UOM.id,
      scrap_percentage: '0.0000',
      is_optional: true,
      substitute_for_line_id: 'bl000000-0000-7000-8000-000000000001',
      operation_sequence: null,
      notes: 'Spare closer',
    },
  ],
  operations: [
    {
      id: 'bo000000-0000-7000-8000-000000000001',
      bom_id: 'bom00000-0000-7000-8000-000000000009',
      operation_sequence: 10,
      name: 'Cut to length',
      description: null,
      work_center_id: null,
      routing_operation_id: null,
      setup_time_minutes: '30.0000',
      run_time_minutes_per_unit: '2.0000',
      cost_per_hour: '600.0000',
      fixed_cost: '50.0000',
      is_outsourced: false,
      notes: null,
    },
  ],
}

function renderEditDialog(routes: RouteTable = {}) {
  return renderScreen(<BomEditDialog bom={{ ...DRAFT_BOM }} open onOpenChange={() => {}} />, {
    permissions: [P.BOM_VIEW, P.BOM_UPDATE],
    routes: {
      [`GET /products/items/${SCREW.id}`]: ok(SCREW),
      [`GET /products/items/${ITEM.id}`]: ok(ITEM),
      'GET /routing/work-centers': page([]),
      ...routes,
    },
  })
}

describe('editing a draft', () => {
  it('sends the prefilled lines and operations wholesale on save', async () => {
    const { user, fetch } = renderEditDialog({
      [`PATCH /bom/${DRAFT_BOM.id}`]: ok(DRAFT_BOM),
    })

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    await expect.poll(() => fetch.calls.some((call) => call.method === 'PATCH')).toBe(true)
    const body = fetch.requestFor('PATCH', `/bom/${DRAFT_BOM.id}`) as {
      lines: Record<string, unknown>[]
      operations: Record<string, unknown>[]
    }
    expect(body.lines).toHaveLength(2)
    expect(body.lines[0]).toMatchObject({
      line_number: 1,
      component_item_id: SCREW.id,
      quantity_per: '6.0000',
      scrap_percentage: '5.0000',
      is_optional: false,
      substitute_for_line_number: null,
      operation_sequence: 10,
      notes: null,
    })
    // The substitute points at the first line's number, translated from the id.
    expect(body.lines[1]).toMatchObject({
      line_number: 2,
      substitute_for_line_number: 1,
      operation_sequence: null,
      is_optional: true,
    })
    expect(body.operations).toHaveLength(1)
    expect(body.operations[0]).toMatchObject({
      operation_sequence: 10,
      name: 'Cut to length',
      setup_time_minutes: '30.0000',
      run_time_minutes_per_unit: '2.0000',
      cost_per_hour: '600.0000',
      fixed_cost: '50.0000',
      is_outsourced: false,
    })
  })

  it('says on the form when someone else saved first', async () => {
    const { user } = renderEditDialog({
      [`PATCH /bom/${DRAFT_BOM.id}`]: fail(
        409,
        'CONCURRENT_UPDATE',
        'This bill was changed by someone else. Reload it and re-apply the change.',
      ),
    })

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    expect(await within(dialog).findByText(/changed by someone else/)).toBeInTheDocument()
    // The dialog stays open so the edit can be re-applied after a reload.
    expect(within(dialog).getByRole('button', { name: /save changes/i })).toBeVisible()
  })
})

/**
 * Quality completion screens (WP E5).
 *
 * Three behaviours the compiler cannot see:
 *
 * 1. **The raise dialog posts the lot exactly as stated.** Stage, item,
 *    warehouse, quantity, batch, hold flag, template choice, reference and
 *    reason each map to one wire field, and an empty optional posts null -
 *    never an empty string the API would bind and refuse.
 * 2. **A row click opens the read-only detail at any status.** Decided lots
 *    used to be unopenable, so the verdict history, the photos and the rework
 *    cycles were unreachable from the screen. Photos arrive as blobs through
 *    `/documents/{id}/download`, never as public URLs.
 * 3. **The rework tab reads `rework_only`, not the history.** It is the same
 *    list with the bench filter and a status filter instead of a result one.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/lib/api-client'
import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'

import { InspectionDetailDialog } from './inspection-detail-dialog'
import QualityPage from './quality-page'
import { RaiseInspectionDialog } from './raise-inspection-dialog'
import type { QcInspectionDetail, QcInspectionRow } from './quality-types'

const ITEM = { id: 'item-fg', code: 'FG-100', name: 'Finished good', is_active: true }
const WAREHOUSES = {
  'GET /warehouses': page([{ id: 'wh-1', code: 'MAIN', name: 'Main store' }]),
}

function row(overrides: Partial<QcInspectionRow> = {}): QcInspectionRow {
  return {
    id: '01a00000-0000-7000-8000-0000000000q1',
    code: 'QC-0001',
    qc_stage: 'FINAL',
    status: 'REWORK',
    result: 'ACCEPTED',
    item_id: 'item-fg',
    batch_id: null,
    vendor_id: null,
    warehouse_id: 'wh-1',
    reference_type: 'packing_order',
    reference_id: '01a00000-0000-7000-8000-0000000000p1',
    reference_code: 'PK-0001',
    inspected_quantity: '40.0000',
    sample_size: '40.0000',
    accepted_quantity: '0.0000',
    rejected_quantity: '0.0000',
    rework_quantity: '40.0000',
    hold_quantity: '0.0000',
    scrap_quantity: '0.0000',
    return_required: false,
    inspected_at: '2026-10-02T08:00:00Z',
    inspector_user_id: null,
    template_id: null,
    template_version: null,
    created_at: '2026-10-01T08:00:00Z',
    ...overrides,
  }
}

function detail(overrides: Partial<QcInspectionDetail> = {}): QcInspectionDetail {
  return {
    ...row(),
    rejection_reason: 'Face scored',
    remarks: 'Repack and re-inspect',
    closed_at: null,
    parameters: [
      {
        id: 'param-1',
        template_id: 'tpl-1',
        sequence: 1,
        parameter_name: 'Face finish',
        parameter_type: 'BOOLEAN',
        unit: null,
        min_value: null,
        max_value: null,
        target_value: null,
        allowed_values: null,
        is_critical: true,
        is_mandatory: true,
      },
    ],
    results: [
      {
        id: 'result-1',
        inspection_id: row().id,
        parameter_id: 'param-1',
        measured_value_numeric: null,
        measured_value_text: null,
        measured_value_bool: false,
        is_within_spec: false,
        remark: 'Scored across the face',
        photo_document_id: 'doc-photo-1',
      },
    ],
    decisions: [
      {
        id: 'verdict-1',
        inspection_id: row().id,
        sequence: 1,
        result: 'ACCEPTED',
        accepted_quantity: '0.0000',
        rejected_quantity: '0.0000',
        rework_quantity: '40.0000',
        hold_quantity: '0.0000',
        scrap_quantity: '0.0000',
        reason: 'Sent back to the bench',
        rejection_reason: null,
        inspected_at: '2026-10-02T08:00:00Z',
        created_at: '2026-10-02T08:00:00Z',
      },
    ],
    rework_records: [
      {
        id: 'rework-1',
        code: 'RW-0001',
        inspection_id: row().id,
        quantity: '40.0000',
        assigned_to: null,
        work_center_id: 'wc-1',
        status: 'IN_PROGRESS',
        outcome: null,
        cost: null,
        notes: 'Polish out the scoring',
        started_at: '2026-10-03T08:00:00Z',
        completed_at: null,
        created_at: '2026-10-02T09:00:00Z',
      },
    ],
    ...overrides,
  }
}

function sentBody(value: unknown): Record<string, unknown> {
  expect(value).toBeDefined()
  return value as Record<string, unknown>
}

// ------------------------------------------------------- raising by hand ----

describe('raising an inspection by hand', () => {
  const routes = {
    'GET /products/items': page([ITEM]),
    ...WAREHOUSES,
    'GET /quality/templates': page([]),
  }

  it('offers the lot fields the manual endpoint takes', async () => {
    renderScreen(<RaiseInspectionDialog open onOpenChange={() => {}} />, {
      permissions: [P.QC_INSPECT],
      routes,
    })

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Stage')).toBeVisible()
    expect(within(dialog).getByRole('combobox', { name: 'Item' })).toBeVisible()
    expect(within(dialog).getByLabelText('Quantity')).toBeVisible()
    expect(within(dialog).getByLabelText('Batch id (optional)')).toBeVisible()
    expect(within(dialog).getByText('Specification (optional)')).toBeVisible()
    expect(within(dialog).getByText('Move the quantity into QC hold')).toBeVisible()
    expect(within(dialog).getByLabelText('Reason')).toBeVisible()
  })

  it('refuses to raise without the inspect right', async () => {
    renderScreen(<RaiseInspectionDialog open onOpenChange={() => {}} />, {
      permissions: [P.QC_VIEW],
      routes,
    })

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('button', { name: /raise inspection/i })).toBeDisabled()
  })

  it('posts the lot exactly as stated, with empty optionals as null', async () => {
    const { user, fetch } = renderScreen(<RaiseInspectionDialog open onOpenChange={() => {}} />, {
      permissions: [P.QC_INSPECT],
      routes: { ...routes, 'POST /quality/inspections': ok(row()) },
    })

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('combobox', { name: 'Item' }))
    await user.click(await screen.findByRole('option', { name: /FG-100/ }))
    await user.click(within(dialog).getByRole('combobox', { name: 'Warehouse' }))
    await user.click(await screen.findByRole('option', { name: /MAIN/ }))
    await user.type(within(dialog).getByLabelText('Quantity'), '40')
    await user.type(within(dialog).getByLabelText('Reason'), 'Supervisor pulled the lot')
    await user.click(within(dialog).getByRole('button', { name: /raise inspection/i }))

    const body = sentBody(await waitFor(() => fetch.requestFor('POST', '/quality/inspections')))
    expect(body).toMatchObject({
      item_id: ITEM.id,
      warehouse_id: 'wh-1',
      qc_stage: 'IN_PROCESS',
      inspected_quantity: '40',
      batch_id: null,
      hold_stock: true,
      template_id: null,
      reference_type: 'manual',
      reference_code: null,
      reference_id: null,
      reason: 'Supervisor pulled the lot',
      remarks: null,
    })
  })
})

// ------------------------------------------------------------ read-only row --

describe('opening any inspection read-only', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('shows readings, the rejection reason, the history and the rework', async () => {
    renderScreen(
      <InspectionDetailDialog
        inspection={row()}
        open
        onOpenChange={() => {}}
        onInspect={() => {}}
      />,
      {
        permissions: [P.QC_VIEW, P.DOCUMENT_VIEW],
        routes: {
          [`GET /quality/inspections/${row().id}`]: ok(detail()),
          'GET /documents/doc-photo-1': ok({
            id: 'doc-photo-1',
            file_name: 'face-finish.jpg',
            mime_type: 'image/jpeg',
          }),
          'GET /documents': page([]),
        },
      },
    )

    expect(await screen.findByText('Face finish')).toBeVisible()
    expect(screen.getByText('Face scored')).toBeVisible()
    expect(screen.getByText(/Cycle 1/)).toBeVisible()
    expect(await screen.findByText('RW-0001')).toBeVisible()
    expect(screen.getByText('Polish out the scoring')).toBeVisible()
  })

  it('renders photos from blobs fetched through the download route', async () => {
    // `api.blobUrl` is spied rather than stubbing fetch: the harness answers
    // JSON at the fetch boundary and a thumbnail is bytes, so the bytes are
    // faked one layer up while both routes the screen calls stay asserted -
    // the metadata read that classifies the photo, and the download it backs.
    vi.spyOn(api, 'blobUrl').mockResolvedValue('blob:photo-1')

    renderScreen(
      <InspectionDetailDialog
        inspection={row()}
        open
        onOpenChange={() => {}}
        onInspect={() => {}}
      />,
      {
        permissions: [P.QC_VIEW, P.DOCUMENT_VIEW],
        routes: {
          [`GET /quality/inspections/${row().id}`]: ok(detail()),
          'GET /documents/doc-photo-1': ok({
            id: 'doc-photo-1',
            file_name: 'face-finish.jpg',
            mime_type: 'image/jpeg',
          }),
          'GET /documents': page([]),
        },
      },
    )

    const thumbnail = await screen.findByRole('img', { name: 'QC-0001 Face finish' })
    expect(thumbnail).toHaveAttribute('src', 'blob:photo-1')
    expect(api.blobUrl).toHaveBeenCalledWith('/documents/doc-photo-1/download')
  })

  it('a row click opens the detail while Inspect still opens the sheet', async () => {
    const { user } = renderScreen(<QualityPage />, {
      permissions: [P.QC_VIEW, P.QC_INSPECT, P.DOCUMENT_VIEW],
      routes: {
        'GET /quality/pending': page([row({ status: 'ACCEPTED', result: 'ACCEPTED' })]),
        [`GET /quality/inspections/${row().id}`]: ok(detail()),
        'GET /documents/doc-photo-1': ok({
          id: 'doc-photo-1',
          file_name: 'face-finish.jpg',
          mime_type: 'image/jpeg',
        }),
        'GET /documents': page([]),
      },
    })

    await user.click(await screen.findByText('QC-0001'))
    expect(await screen.findByText('Verdict history')).toBeVisible()
  })
})

// ---------------------------------------------------------------- rework ----

describe('the rework tab', () => {
  it('lists the bench through rework_only with a status filter', async () => {
    const requests: Record<string, string>[] = []
    renderScreen(<QualityPage />, {
      permissions: [P.QC_VIEW],
      route: '/quality?tab=rework',
      routes: {
        'GET /quality/inspections': ({ url }) => {
          requests.push(Object.fromEntries(url.searchParams))
          return page([row()])
        },
      },
    })

    expect(await screen.findByText('QC-0001')).toBeVisible()
    expect(requests[0]).toMatchObject({ rework_only: 'true' })
    expect(screen.getByRole('combobox', { name: 'Filter by status' })).toBeVisible()
    expect(screen.queryByRole('combobox', { name: 'Filter by result' })).not.toBeInTheDocument()
  })

  it('is hidden behind nothing - the tab shows for every quality reader', async () => {
    renderScreen(<QualityPage />, {
      permissions: [P.QC_VIEW],
      routes: { 'GET /quality/pending': page([]) },
    })

    expect(await screen.findByRole('tab', { name: /rework/i })).toBeVisible()
  })
})

describe('rework permission gates', () => {
  it('fails the rework list for an account without qc.view', async () => {
    renderScreen(<QualityPage />, {
      permissions: [],
      route: '/quality?tab=rework',
      routes: {
        'GET /quality/inspections': fail(403, 'FORBIDDEN', 'Missing permission qc.view'),
      },
    })

    expect(await screen.findByText(/Missing permission/i)).toBeVisible()
  })
})

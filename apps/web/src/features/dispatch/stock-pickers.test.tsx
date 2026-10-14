import { screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import { PICKER_PAGE_SIZE } from '@/lib/constants'
import { P } from '@/lib/permissions'
import { fail, page, renderScreen, type RouteRequest } from '@/test/harness'

import { BatchSelect } from './stock-pickers'

function batch(number: number, itemId = 'item') {
  return {
    id: `batch-${number}`,
    item_id: itemId,
    batch_number: `BATCH-${number}`,
    status: 'ACTIVE',
    expires_on: null,
  }
}

function ControlledBatch({ initial = '', itemId = 'item' }: { initial?: string; itemId?: string }) {
  const [value, setValue] = useState(initial)
  return <BatchSelect id="batch" itemId={itemId} value={value} onChange={setValue} />
}

describe('batch selection from the complete server list', () => {
  it('can select the 51st batch through bounded server pages', async () => {
    const urls: URL[] = []
    const h = renderScreen(<ControlledBatch />, {
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/batches': ({ url }: RouteRequest) => {
          urls.push(url)
          const requested = Number(url.searchParams.get('page') ?? 1)
          const rows = Array.from({ length: PICKER_PAGE_SIZE }, (_, i) =>
            batch((requested - 1) * PICKER_PAGE_SIZE + i + 1),
          )
          return page(rows, {
            page: requested,
            limit: PICKER_PAGE_SIZE,
            has_next: requested < 3,
            has_previous: requested > 1,
            total: null,
            total_pages: null,
          })
        },
      },
    })
    await h.user.click(await screen.findByRole('combobox', { name: 'Batch' }))
    expect(await screen.findByRole('option', { name: 'BATCH-1' })).toBeVisible()
    await h.user.click(screen.getByRole('button', { name: 'Next batch page' }))
    expect(await screen.findByRole('option', { name: 'BATCH-21' })).toBeVisible()
    await h.user.click(screen.getByRole('button', { name: 'Next batch page' }))
    await h.user.click(await screen.findByRole('option', { name: 'BATCH-51' }))
    expect(screen.getByRole('combobox', { name: 'Batch' })).toHaveTextContent('BATCH-51')
    expect(urls.map((url) => url.searchParams.get('page'))).toEqual(['1', '2', '3'])
    expect(urls.every((url) => url.searchParams.get('limit') === String(PICKER_PAGE_SIZE))).toBe(
      true,
    )

    // Moving away from the selected page keeps the label and selected identity.
    await h.user.click(screen.getByRole('combobox', { name: 'Batch' }))
    await h.user.click(screen.getByRole('button', { name: 'Previous batch page' }))
    expect(await screen.findByRole('option', { name: 'BATCH-21' })).toBeVisible()
    expect(screen.getByRole('combobox', { name: 'Batch' })).toHaveTextContent('BATCH-51')
  })

  it('searches the server and resets pagination instead of filtering the first page locally', async () => {
    const urls: URL[] = []
    const h = renderScreen(<ControlledBatch />, {
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/batches': ({ url }: RouteRequest) => {
          urls.push(url)
          return url.searchParams.get('search')
            ? page([batch(51)], { has_next: false })
            : page([batch(1)], { has_next: true })
        },
      },
    })
    await h.user.click(await screen.findByRole('combobox', { name: 'Batch' }))
    await screen.findByRole('option', { name: 'BATCH-1' })
    await h.user.click(screen.getByRole('button', { name: 'Next batch page' }))
    await waitFor(() => expect(urls.some((url) => url.searchParams.get('page') === '2')).toBe(true))
    await h.user.type(screen.getByRole('textbox', { name: 'Search batches' }), 'legacy lot')
    await h.user.click(await screen.findByRole('option', { name: 'BATCH-51' }))
    const searched = [...urls]
      .reverse()
      .find((url) => url.searchParams.get('search') === 'legacy lot')
    expect(searched?.searchParams.get('page')).toBe('1')
    expect(searched?.searchParams.get('item_id')).toBe('item')
    expect(screen.getByRole('combobox', { name: 'Batch' })).toHaveTextContent('BATCH-51')
  })

  it('resolves a restored selection in one row and retains it outside the page/search results', async () => {
    const urls: URL[] = []
    const h = renderScreen(<ControlledBatch initial="batch-51" />, {
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/batches': ({ url }: RouteRequest) => {
          urls.push(url)
          return url.searchParams.get('batch_id') ? page([batch(51)]) : page([batch(1)])
        },
      },
    })
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Batch' })).toHaveTextContent('BATCH-51'),
    )
    expect(urls[0]!.searchParams.get('batch_id')).toBe('batch-51')
    expect(urls[0]!.searchParams.get('item_id')).toBe('item')
    expect(urls[0]!.searchParams.get('limit')).toBe('1')
    await h.user.click(screen.getByRole('combobox', { name: 'Batch' }))
    expect(await screen.findByRole('option', { name: 'BATCH-1' })).toBeVisible()
    expect(screen.getByRole('combobox', { name: 'Batch' })).toHaveTextContent('BATCH-51')
    await h.user.keyboard('{Escape}')
    await h.user.click(screen.getByRole('button', { name: 'Clear batch' }))
    expect(screen.getByRole('combobox', { name: 'Batch' })).toHaveTextContent('Choose the batch')
  })

  it('resets item/search/page state and never reuses another item’s selected label', async () => {
    const urls: URL[] = []
    const h = renderScreen(<ControlledBatch initial="batch-51" />, {
      permissions: [P.INVENTORY_VIEW],
      routes: {
        'GET /inventory/batches': ({ url }: RouteRequest) => {
          urls.push(url)
          return url.searchParams.get('item_id') === 'other' ? page([]) : page([batch(51)])
        },
      },
    })
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Batch' })).toHaveTextContent('BATCH-51'),
    )
    h.rerender(<ControlledBatch initial="batch-51" itemId="other" />)
    await waitFor(() =>
      expect(urls.some((url) => url.searchParams.get('item_id') === 'other')).toBe(true),
    )
    expect(screen.getByRole('combobox', { name: 'Batch' })).not.toHaveTextContent('BATCH-51')
    await h.user.click(screen.getByRole('combobox', { name: 'Batch' }))
    expect(await screen.findByText('No batch of this item is on record.')).toBeVisible()
    expect(screen.getByRole('textbox', { name: 'Search batches' })).toHaveValue('')
  })

  it('shows lookup errors as errors rather than claiming no stock batches exist', async () => {
    const h = renderScreen(<ControlledBatch />, {
      permissions: [P.INVENTORY_VIEW],
      routes: { 'GET /inventory/batches': fail(403, 'FORBIDDEN', 'Batch access denied') },
    })
    await h.user.click(await screen.findByRole('combobox', { name: 'Batch' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Batch access denied')
    expect(screen.queryByText('No batch of this item is on record.')).toBeNull()
  })
})

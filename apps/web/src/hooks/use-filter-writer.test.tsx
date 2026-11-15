import { screen } from '@testing-library/react'
import { useSearchParams } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { renderScreen } from '@/test/harness'

import { cleared, useFilterWriter } from './use-filter-writer'

function Filters({ pageParam }: { pageParam?: string | null }) {
  const [params] = useSearchParams()
  const write = useFilterWriter(pageParam)
  return (
    <>
      <output aria-label="URL parameters">{params.toString()}</output>
      <button onClick={() => write({ status: 'OPEN', vendor_id: 'v-2' })}>Narrow</button>
      <button onClick={() => write(cleared(['status', 'vendor_id', 'q']))}>Clear</button>
      <button onClick={() => write({ spec: ['finish=PVD', 'size=96'] })}>Specifications</button>
      <button onClick={() => write({ spec: [] })}>No specifications</button>
      <button onClick={() => write({ status: '', page: 3 })}>Blank and number</button>
    </>
  )
}

const url = () => screen.getByLabelText('URL parameters').textContent

describe('useFilterWriter', () => {
  it('writes every key in one navigation and sends the list back to its first page', async () => {
    const { user } = renderScreen(<Filters />, { route: '/?status=DRAFT&page=4&tab=orders' })
    await user.click(screen.getByRole('button', { name: 'Narrow' }))
    expect(url()).toBe('status=OPEN&tab=orders&vendor_id=v-2')
  })

  it('clears a set of filters at once, leaving the rest of the URL alone', async () => {
    const { user } = renderScreen(<Filters />, {
      route: '/?status=OPEN&vendor_id=v-2&q=late&page=2&tab=orders',
    })
    await user.click(screen.getByRole('button', { name: 'Clear' }))
    expect(url()).toBe('tab=orders')
  })

  it("resets a namespaced list's own page, not another list's", async () => {
    const { user } = renderScreen(<Filters pageParam="followups_page" />, {
      route: '/?page=3&followups_page=5',
    })
    await user.click(screen.getByRole('button', { name: 'Narrow' }))
    expect(url()).toBe('page=3&status=OPEN&vendor_id=v-2')
  })

  it('keeps every page where it is when told the write narrows nothing', async () => {
    const { user } = renderScreen(<Filters pageParam={null} />, { route: '/?page=3' })
    await user.click(screen.getByRole('button', { name: 'Narrow' }))
    expect(url()).toBe('page=3&status=OPEN&vendor_id=v-2')
  })

  it('writes a multi-valued key as one entry per value, and an empty list as none', async () => {
    const { user } = renderScreen(<Filters />, { route: '/?spec=colour=BLACK&page=2' })
    await user.click(screen.getByRole('button', { name: 'Specifications' }))
    expect(url()).toBe('spec=finish%3DPVD&spec=size%3D96')
    await user.click(screen.getByRole('button', { name: 'No specifications' }))
    expect(url()).toBe('')
  })

  it('removes a blank value and writes a number as text', async () => {
    const { user } = renderScreen(<Filters pageParam={null} />, { route: '/?status=OPEN' })
    await user.click(screen.getByRole('button', { name: 'Blank and number' }))
    expect(url()).toBe('page=3')
  })
})

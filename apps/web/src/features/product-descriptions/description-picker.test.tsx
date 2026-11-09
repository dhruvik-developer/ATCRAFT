import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { picker } from '@/lib/messages'
import { queryKeys } from '@/lib/query-client'
import { page, renderScreen, type RouteBody } from '@/test/harness'

import { DescriptionPicker } from './description-picker'

type Choice = { id: string; code: string; name: string }

const finish: Choice = { id: 'finish', code: 'FIN', name: 'Finish' }

function Picker() {
  return (
    <DescriptionPicker<Choice>
      label="Specification"
      path="/products/items/item-1/specification-attributes"
      queryKey={(params) => queryKeys.items.specificationOptions('item-1', params)}
      onChoose={() => undefined}
    />
  )
}

describe('description picker', () => {
  it('says a read in flight the way every search picker does, then offers the choices', async () => {
    let answer!: (body: RouteBody) => void
    const pending = new Promise<RouteBody>((resolve) => {
      answer = resolve
    })
    renderScreen(<Picker />, {
      routes: { 'GET /products/items/item-1/specification-attributes': () => pending },
    })

    const choice = screen.getByRole('combobox', { name: 'Specification' })
    expect(await screen.findByRole('option', { name: picker.searching })).toBeInTheDocument()
    expect(choice).toBeDisabled()

    answer(page([finish]))

    expect(await screen.findByRole('option', { name: 'Choose specification' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'FIN - Finish' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: picker.searching })).toBeNull()
  })
})

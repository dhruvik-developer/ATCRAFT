import { act, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteBody } from '@/test/harness'
import {
  itemSpecifications,
  itemSpecificationValue,
  specificationAttribute,
} from '@/test/product-description-fixtures'

import { ItemSpecificationsPanel } from './item-specifications-panel'

const keys = specificationAttribute({
  id: 'keys',
  code: 'KEYS',
  name: 'Keys included',
  value_type: 'NUMBER',
  options: [],
})
const fire = specificationAttribute({
  id: 'fire',
  code: 'FIRE',
  name: 'Fire rated',
  value_type: 'BOOLEAN',
  options: [],
})
const material = specificationAttribute()
const read = itemSpecifications({
  values: [
    itemSpecificationValue(keys, { value_number: '0' }),
    itemSpecificationValue(fire, { value_bool: false }),
  ],
  template: [keys, fire, material].map((attribute) => ({
    attribute,
    is_required: true,
    source_category_ids: ['parent', 'category'],
    sort_order: attribute.sort_order,
    missing_required: attribute.id === material.id,
  })),
  missing_required_count: 1,
})

describe('item specification editor', () => {
  it('shows zero, false and completeness warnings to a reader without master permissions', async () => {
    const { fetch } = renderScreen(<ItemSpecificationsPanel itemId="item-1" />, {
      permissions: [P.ITEM_VIEW],
      routes: { 'GET /products/items/item-1/specifications': ok(read) },
    })
    expect(await screen.findByText('No')).toBeVisible()
    expect(screen.getByText('0')).toBeVisible()
    expect(screen.getByText(/This is a completeness warning/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Save specifications' })).toBeNull()
    expect(fetch.calls.some((call) => call.path.includes('specification-attributes'))).toBe(false)
  })

  it('saves a full typed replacement with zero/false even when a required attribute remains blank', async () => {
    let complete!: (body: RouteBody) => void
    const pending = new Promise<RouteBody>((resolve) => {
      complete = resolve
    })
    const { user, fetch } = renderScreen(<ItemSpecificationsPanel itemId="item-1" />, {
      permissions: [P.ITEM_VIEW, P.ITEM_UPDATE],
      routes: {
        'GET /products/items/item-1/specifications': ok(read),
        'GET /products/items/item-1/specification-attributes': page([keys, fire, material]),
        'PUT /products/items/item-1/specifications': () => pending,
      },
    })
    const no = await screen.findByRole('combobox', { name: 'Fire rated' })
    expect(no).toHaveValue('false')
    expect(screen.getByRole('textbox', { name: 'Keys included' })).toHaveValue('0')
    await user.click(screen.getByRole('button', { name: 'Save specifications' }))
    await waitFor(() =>
      expect(fetch.requestFor('PUT', '/products/items/item-1/specifications')).toEqual({
        values: [
          { specification_attribute_id: keys.id, value_number: '0' },
          { specification_attribute_id: fire.id, value_bool: false },
        ],
      }),
    )
    expect(no).toBeDisabled()
    expect(screen.getByRole('button', { name: /Save specifications/ })).toBeDisabled()
    expect(fetch.calls.some((call) => call.path === '/products/specification-attributes')).toBe(
      false,
    )
    await act(async () => {
      complete(ok(read))
    })
    await waitFor(() => expect(no).toBeEnabled())
    expect(screen.getByText(/This is a completeness warning/)).toBeVisible()
  })

  it('removes cleared values and keeps an exact signed decimal string', async () => {
    const { user, fetch } = renderScreen(<ItemSpecificationsPanel itemId="item-1" />, {
      permissions: [P.ITEM_VIEW, P.ITEM_UPDATE],
      routes: {
        'GET /products/items/item-1/specifications': ok(read),
        'GET /products/items/item-1/specification-attributes': page([]),
        'PUT /products/items/item-1/specifications': ok(read),
      },
    })
    const number = await screen.findByRole('textbox', { name: 'Keys included' })
    await user.clear(number)
    await user.paste('-99999999999999.9999')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Fire rated' }), '__none')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Material' }), 'Brass')
    await user.click(screen.getByRole('button', { name: 'Save specifications' }))
    await waitFor(() =>
      expect(fetch.requestFor('PUT', '/products/items/item-1/specifications')).toEqual({
        values: [
          { specification_attribute_id: keys.id, value_number: '-99999999999999.9999' },
          { specification_attribute_id: material.id, value_option: 'Brass' },
        ],
      }),
    )
  })

  it('marks malformed decimals locally without sending a replacement', async () => {
    const { user, fetch } = renderScreen(<ItemSpecificationsPanel itemId="item-1" />, {
      permissions: [P.ITEM_VIEW, P.ITEM_UPDATE],
      routes: {
        'GET /products/items/item-1/specifications': ok(read),
        'GET /products/items/item-1/specification-attributes': page([]),
      },
    })
    const number = await screen.findByRole('textbox', { name: 'Keys included' })
    await user.clear(number)
    await user.paste('0.00001')
    await user.click(screen.getByRole('button', { name: 'Save specifications' }))
    expect(screen.getByText(/at most 14 whole digits and 4 decimal places/)).toBeVisible()
    expect(number).toHaveAttribute('aria-invalid', 'true')
    expect(fetch.calls.some((call) => call.method === 'PUT')).toBe(false)
  })

  it('preserves drafts and associates an API field refusal with its typed control', async () => {
    const { user } = renderScreen(<ItemSpecificationsPanel itemId="item-1" />, {
      permissions: [P.ITEM_VIEW, P.ITEM_UPDATE],
      routes: {
        'GET /products/items/item-1/specifications': ok(read),
        'GET /products/items/item-1/specification-attributes': page([]),
        'PUT /products/items/item-1/specifications': fail(
          422,
          'VALIDATION_ERROR',
          'Invalid value',
          { fields: { 'values.0.value_number': ['This measure is no longer permitted.'] } },
        ),
      },
    })
    const number = await screen.findByRole('textbox', { name: 'Keys included' })
    await user.click(screen.getByRole('button', { name: 'Save specifications' }))
    await waitFor(() => expect(number).toHaveAttribute('aria-invalid', 'true'))
    expect(screen.getAllByText('This measure is no longer permitted.').length).toBeGreaterThan(0)
    expect(number).toHaveValue('0')
    expect(screen.getByRole('combobox', { name: 'Fire rated' })).toHaveValue('false')
  })

  it('adds an attribute through the item-authorized paginated metadata lookup', async () => {
    const text = specificationAttribute({
      id: 'notes',
      code: 'DESCRIPTION',
      name: 'Technical note',
      value_type: 'TEXT',
      options: [],
    })
    const { user, fetch } = renderScreen(<ItemSpecificationsPanel itemId="item-1" />, {
      permissions: [P.ITEM_VIEW, P.ITEM_UPDATE],
      routes: {
        'GET /products/items/item-1/specifications': ok(itemSpecifications()),
        'GET /products/items/item-1/specification-attributes': ({ url }) =>
          page(url.searchParams.get('page') === '2' ? [text] : [], {
            has_next: url.searchParams.get('page') !== '2',
          }),
        'PUT /products/items/item-1/specifications': ok(itemSpecifications()),
      },
    })
    await user.click(await screen.findByRole('button', { name: 'More choices' }))
    await screen.findByRole('option', { name: 'DESCRIPTION - Technical note' })
    await user.selectOptions(screen.getByRole('combobox', { name: 'Specification' }), text.id)
    await user.click(screen.getByRole('button', { name: 'Choose specification' }))
    await user.click(screen.getByRole('textbox', { name: 'Technical note' }))
    await user.paste('Outdoor application')
    await user.click(screen.getByRole('button', { name: 'Save specifications' }))
    await waitFor(() =>
      expect(fetch.requestFor('PUT', '/products/items/item-1/specifications')).toEqual({
        values: [{ specification_attribute_id: text.id, value_text: 'Outdoor application' }],
      }),
    )
  })
})

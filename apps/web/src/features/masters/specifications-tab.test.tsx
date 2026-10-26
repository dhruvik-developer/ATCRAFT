import { act, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteBody } from '@/test/harness'
import { specificationAttribute } from '@/test/product-description-fixtures'

import MastersPage from './masters-page'
import { CategorySpecificationsPanel } from './category-specifications-panel'
import { SpecificationFormDialog, SpecificationRetireDialog } from './specification-dialogs'

const attribute = specificationAttribute()
const category = {
  id: 'category',
  code: 'LOCKS',
  name: 'Lock bodies',
  parent_id: null,
  path: '/category/',
  depth: 0,
  is_active: true,
}

describe('specification masters', () => {
  it('renders the actual masters tab read-only with the existing attribute-view right', async () => {
    const { fetch } = renderScreen(<MastersPage />, {
      route: '/masters?tab=specifications',
      permissions: [P.ITEM_ATTRIBUTE_VIEW],
      routes: { 'GET /products/specification-attributes': page([attribute]) },
    })
    expect(await screen.findByRole('tab', { name: 'Specifications' })).toBeVisible()
    expect(await screen.findByText('MATERIAL')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'New specification' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit MATERIAL' })).toBeNull()
    expect(fetch.calls.some((call) => call.path === '/products/categories')).toBe(false)
  })

  it('creates a governed Option attribute, then waits for the request before allowing dismissal', async () => {
    let complete!: (body: RouteBody) => void
    const response = new Promise<RouteBody>((resolve) => {
      complete = resolve
    })
    const close = vi.fn()
    const { user, fetch } = renderScreen(<SpecificationFormDialog open onOpenChange={close} />, {
      permissions: [P.ITEM_ATTRIBUTE_MANAGE],
      routes: { 'POST /products/specification-attributes': () => response },
    })
    await user.click(screen.getByRole('textbox', { name: 'Code' }))
    await user.paste('MATERIAL')
    await user.click(screen.getByRole('textbox', { name: 'Name' }))
    await user.paste('Material')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Value type' }), 'OPTION')
    await user.click(screen.getByRole('button', { name: 'Create specification' }))
    expect(await screen.findByText('Enter at least one option, one per line.')).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
    await user.click(screen.getByRole('textbox', { name: 'Permitted options' }))
    await user.paste('Brass\n Zinc Alloy ')
    await user.click(screen.getByRole('checkbox', { name: 'Available as a catalog filter' }))
    await user.click(screen.getByRole('button', { name: 'Create specification' }))
    await waitFor(() =>
      expect(fetch.requestFor('POST', '/products/specification-attributes')).toEqual({
        code: 'MATERIAL',
        name: 'Material',
        value_type: 'OPTION',
        unit: null,
        options: ['Brass', 'Zinc Alloy'],
        is_filterable: true,
        is_active: true,
        sort_order: 0,
      }),
    )
    expect(screen.getByRole('button', { name: /Create specification/ })).toBeDisabled()
    await user.keyboard('{Escape}')
    expect(close).not.toHaveBeenCalled()
    await act(async () => {
      complete(ok(attribute))
    })
    await waitFor(() => expect(close).toHaveBeenCalledWith(false))
  })

  it('rejects duplicate options and shows a backend refusal without discarding the edit', async () => {
    const { user, fetch } = renderScreen(
      <SpecificationFormDialog open attribute={attribute} onOpenChange={() => {}} />,
      {
        permissions: [P.ITEM_ATTRIBUTE_MANAGE],
        routes: {
          'PATCH /products/specification-attributes/attribute-1': fail(
            422,
            'VALIDATION_ERROR',
            'Attribute is in use',
            { fields: { options: ['An item still uses Zinc Alloy.'] } },
          ),
        },
      },
    )
    expect(screen.getByRole('textbox', { name: 'Code' })).toBeDisabled()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save specification' })).toBeEnabled(),
    )
    const options = screen.getByRole('textbox', { name: 'Permitted options' })
    await user.clear(options)
    await user.paste('Brass\nBrass')
    await user.click(screen.getByRole('button', { name: 'Save specification' }))
    expect(await screen.findByText('Each option must appear only once.')).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'PATCH')).toBe(false)
    await user.clear(options)
    await user.paste('Brass')
    await user.click(screen.getByRole('button', { name: 'Save specification' }))
    await waitFor(() =>
      expect(screen.getAllByText('An item still uses Zinc Alloy.').length).toBeGreaterThan(0),
    )
    expect(options).toHaveValue('Brass')
    expect(
      fetch.requestFor('PATCH', '/products/specification-attributes/attribute-1'),
    ).not.toHaveProperty('code')
  })

  it('retains a refused retirement reason and displays its authoritative explanation', async () => {
    let reason: string | null = null
    const { user } = renderScreen(
      <SpecificationRetireDialog attribute={attribute} onOpenChange={() => {}} />,
      {
        permissions: [P.ITEM_ATTRIBUTE_MANAGE],
        routes: {
          'DELETE /products/specification-attributes/attribute-1': ({ url }) => {
            reason = url.searchParams.get('reason')
            return fail(
              422,
              'REFERENCED_RECORD_IN_USE',
              'Remove category templates before retiring this specification.',
            )
          },
        },
      },
    )
    await user.click(screen.getByRole('textbox', { name: 'Reason' }))
    await user.paste('Vocabulary no longer used')
    await user.click(screen.getByRole('button', { name: 'Retire specification' }))
    expect(
      await screen.findByText('Remove category templates before retiring this specification.'),
    ).toBeVisible()
    expect(reason).toBe('Vocabulary no longer used')
    expect(screen.getByRole('textbox', { name: 'Reason' })).toHaveValue('Vocabulary no longer used')
  })
})

describe('category specification templates', () => {
  it('edits the category template without category or item permissions', async () => {
    const fire = specificationAttribute({
      id: 'fire',
      code: 'FIRE',
      name: 'Fire rated',
      value_type: 'BOOLEAN',
      options: [],
    })
    const initial = {
      category_id: category.id,
      specifications: [
        { id: 'template', category_id: category.id, attribute, is_required: true, sort_order: 0 },
      ],
    }
    const { user, fetch } = renderScreen(<CategorySpecificationsPanel />, {
      permissions: [P.ITEM_ATTRIBUTE_VIEW, P.ITEM_ATTRIBUTE_MANAGE],
      routes: {
        'GET /products/specification-categories': page([category]),
        'GET /products/categories/category/specifications': ok(initial),
        'GET /products/specification-attributes': page([attribute, fire]),
        'PUT /products/categories/category/specifications': ok(initial),
      },
    })
    await screen.findByRole('option', { name: 'LOCKS - Lock bodies' })
    await user.selectOptions(screen.getByRole('combobox', { name: 'Category' }), category.id)
    await user.click(screen.getByRole('button', { name: 'Choose category' }))
    expect(await screen.findByRole('heading', { name: 'LOCKS - Lock bodies' })).toBeVisible()
    await user.click(await screen.findByRole('checkbox', { name: 'Required warning' }))
    await screen.findByRole('option', { name: 'FIRE - Fire rated' })
    await user.selectOptions(screen.getByRole('combobox', { name: 'Specification' }), fire.id)
    await user.click(screen.getByRole('button', { name: 'Choose specification' }))
    await user.click(screen.getByRole('button', { name: 'Save category template' }))
    await waitFor(() =>
      expect(fetch.requestFor('PUT', '/products/categories/category/specifications')).toEqual({
        specifications: [
          { specification_attribute_id: attribute.id, is_required: false, sort_order: 0 },
          { specification_attribute_id: fire.id, is_required: false, sort_order: 1 },
        ],
      }),
    )
    expect(fetch.calls.some((call) => call.path === '/products/categories')).toBe(false)
    expect(fetch.calls.some((call) => call.path === '/products/items')).toBe(false)
  })

  it('allows template viewing without management controls', async () => {
    const { user, fetch } = renderScreen(<CategorySpecificationsPanel />, {
      permissions: [P.ITEM_ATTRIBUTE_VIEW],
      routes: {
        'GET /products/specification-categories': page([category]),
        'GET /products/categories/category/specifications': ok({
          category_id: category.id,
          specifications: [
            {
              id: 'template',
              category_id: category.id,
              attribute,
              is_required: true,
              sort_order: 0,
            },
          ],
        }),
      },
    })
    await screen.findByRole('option', { name: 'LOCKS - Lock bodies' })
    await user.selectOptions(screen.getByRole('combobox', { name: 'Category' }), category.id)
    await user.click(screen.getByRole('button', { name: 'Choose category' }))
    expect(await screen.findByRole('checkbox', { name: 'Required warning' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Save category template' })).toBeNull()
    expect(fetch.calls.some((call) => call.path === '/products/specification-attributes')).toBe(
      false,
    )
  })
})

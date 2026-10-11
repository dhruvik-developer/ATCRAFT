/**
 * The organisation screen: which registers an account is shown, and what the
 * cost-centre and department forms actually send.
 *
 * Three properties matter here and none is visible in a screenshot.
 *
 * **The route and each tab follow the right their endpoint enforces.** The page
 * is reachable on `company.view` or `cost_centre.view`, and every tab is gated
 * on its own register's right - an account holding `cost_centre.view` alone
 * must land on the Cost centres tab, and must not be shown a Company tab whose
 * request could only 403.
 *
 * **A parent comes from the whole tree.** The department form offers the
 * current page of departments as candidate parents; the cost-centre form must
 * not repeat that, because a parent two pages away is still a parent. The
 * picker reads the full reference list, offers only active centres, and never
 * a centre's own subtree.
 *
 * **A hidden control never clears a saved value.** An account without
 * `cost_centre.view` is shown no cost-centre picker, and an edit it makes must
 * send back the centre the record already had rather than null.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen, type RouteBody, type RouteResponder } from '@/test/harness'

import type { DepartmentRow } from './admin-types'
import type { CostCentreRow } from './cost-centre-model'
import OrganisationPage from './organisation-page'

const STAMP = '2026-09-01T09:00:00Z'

function centre(overrides: Partial<CostCentreRow> = {}): CostCentreRow {
  return {
    id: 'cc-prod',
    code: 'CC-PROD',
    name: 'Production',
    cost_centre_type: 'PRODUCTION',
    parent_id: null,
    plant_id: null,
    head_user_id: null,
    description: null,
    is_active: true,
    created_at: STAMP,
    updated_at: STAMP,
    ...overrides,
  }
}

const PRODUCTION = centre()
const MACHINING = centre({
  id: 'cc-mach',
  code: 'CC-MACH',
  name: 'Machining',
  parent_id: 'cc-prod',
})
const CNC = centre({ id: 'cc-cnc', code: 'CC-MACH-CNC', name: 'CNC cell', parent_id: 'cc-mach' })
const SALES = centre({
  id: 'cc-sales',
  code: 'CC-SALES',
  name: 'Sales',
  cost_centre_type: 'SELLING',
})
const RETIRED_ZONE = centre({
  id: 'cc-zone-old',
  code: 'CC-ZONE-OLD',
  name: 'Old sales zone',
  cost_centre_type: 'SELLING',
  is_active: false,
})

/** Every live centre - what the reference list behind each picker returns. */
const TREE = [PRODUCTION, MACHINING, CNC, SALES, RETIRED_ZONE]

/**
 * `GET /organization/cost-centres` answers two questions on this screen: a page
 * of the register (it carries `page`) and the full reference list behind the
 * pickers (it does not). The page holds one row on purpose, so a parent offered
 * from it alone would be visibly missing.
 */
function costCentres(rows: CostCentreRow[] = [PRODUCTION]): RouteResponder {
  return ({ url }) => (url.searchParams.has('page') ? page(rows) : page(TREE))
}

describe('who reaches the cost centres', () => {
  it('lands an account holding cost_centre.view alone on its tab, and on nothing else', async () => {
    const { fetch } = renderScreen(<OrganisationPage />, {
      permissions: [P.COST_CENTRE_VIEW],
      route: '/admin/organisation',
      routes: { 'GET /organization/cost-centres': costCentres() },
    })

    expect(await screen.findByRole('tab', { name: 'Cost centres' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(await screen.findByText('CC-PROD')).toBeVisible()
    expect(screen.queryByRole('tab', { name: 'Company' })).not.toBeInTheDocument()
    expect(fetch.calls.some((call) => call.path === '/organization/company')).toBe(false)
  })

  it('shows no Cost centres tab, and asks for no centre, without the right', async () => {
    const { fetch } = renderScreen(<OrganisationPage />, {
      permissions: [P.DEPARTMENT_VIEW],
      route: '/admin/organisation?tab=cost-centres',
      routes: { 'GET /organization/departments': page([]) },
    })

    // A bookmarked tab the account may not open falls back to one it may.
    expect(await screen.findByRole('tab', { name: 'Departments' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(screen.queryByRole('tab', { name: 'Cost centres' })).not.toBeInTheDocument()
    expect(fetch.calls.some((call) => call.path.startsWith('/organization/cost-centres'))).toBe(
      false,
    )
  })

  it('offers no New, Edit or Retire to an account that may only read', async () => {
    renderScreen(<OrganisationPage />, {
      permissions: [P.COST_CENTRE_VIEW],
      route: '/admin/organisation?tab=cost-centres',
      routes: { 'GET /organization/cost-centres': costCentres([PRODUCTION, MACHINING]) },
    })

    expect(await screen.findByText('CC-MACH')).toBeVisible()
    expect(screen.queryByRole('button', { name: /new cost centre/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /actions for/i })).not.toBeInTheDocument()
  })

  it('names a parent from the whole tree, not from the page on screen', async () => {
    renderScreen(<OrganisationPage />, {
      permissions: [P.COST_CENTRE_VIEW],
      route: '/admin/organisation?tab=cost-centres',
      // CNC's parent, Machining, is not on this page of the register.
      routes: { 'GET /organization/cost-centres': costCentres([CNC]) },
    })

    expect(await screen.findByText('CC-MACH - Machining')).toBeVisible()
  })
})

describe('creating a cost centre', () => {
  const routes = (post: RouteBody = ok(centre({ id: 'cc-buff', code: 'CC-BUFF' }))) => ({
    'GET /organization/cost-centres': costCentres(),
    'POST /organization/cost-centres': post,
  })

  it('sends the code, the type and a parent chosen from the whole tree', async () => {
    const { user, fetch } = renderScreen(<OrganisationPage />, {
      permissions: [P.COST_CENTRE_VIEW, P.COST_CENTRE_MANAGE],
      route: '/admin/organisation?tab=cost-centres',
      routes: routes(),
    })

    await user.click(await screen.findByRole('button', { name: /new cost centre/i }))
    const dialog = await screen.findByRole('dialog')

    await user.type(within(dialog).getByRole('textbox', { name: 'Code' }), 'CC-BUFF')
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'Buffing and polishing')
    await user.click(within(dialog).getByRole('combobox', { name: 'Type' }))
    await user.click(await screen.findByRole('option', { name: 'Production' }))

    await user.click(within(dialog).getByRole('combobox', { name: 'Parent cost centre' }))
    // Machining is not on the page of the register being shown, and is offered
    // all the same; the switched-off zone is not offered at all.
    expect(screen.queryByRole('option', { name: /CC-ZONE-OLD/ })).not.toBeInTheDocument()
    await user.click(await screen.findByRole('option', { name: 'CC-MACH - Machining' }))

    await user.type(
      within(dialog).getByRole('textbox', { name: 'Description' }),
      '  Satin and mirror finish  ',
    )
    await user.click(within(dialog).getByRole('button', { name: /create cost centre/i }))

    const body = await waitFor(() => fetch.requestFor('POST', '/organization/cost-centres'))
    expect(body).toEqual({
      code: 'CC-BUFF',
      name: 'Buffing and polishing',
      cost_centre_type: 'PRODUCTION',
      parent_id: 'cc-mach',
      plant_id: null,
      head_user_id: null,
      description: 'Satin and mirror finish',
      is_active: true,
    })
  })

  it('refuses a centre with no type before anything reaches the network', async () => {
    // The API requires the type and deliberately defaults nothing, so neither
    // does the form: a preselected type would misfile every centre whose
    // creator did not notice it.
    const { user, fetch } = renderScreen(<OrganisationPage />, {
      permissions: [P.COST_CENTRE_VIEW, P.COST_CENTRE_MANAGE],
      route: '/admin/organisation?tab=cost-centres',
      routes: routes(),
    })

    await user.click(await screen.findByRole('button', { name: /new cost centre/i }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: 'Code' }), 'CC-BUFF')
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'Buffing and polishing')
    await user.click(within(dialog).getByRole('button', { name: /create cost centre/i }))

    expect(
      await within(dialog).findByText('Choose how this centre is grouped on reports'),
    ).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('puts a duplicate-code refusal on the code', async () => {
    const { user } = renderScreen(<OrganisationPage />, {
      permissions: [P.COST_CENTRE_VIEW, P.COST_CENTRE_MANAGE],
      route: '/admin/organisation?tab=cost-centres',
      routes: routes(
        fail(409, 'DUPLICATE', "Cost centre code 'CC-PROD' is already used in this company.", {
          fields: { code: ['That code is already used in this company.'] },
        }),
      ),
    })

    await user.click(await screen.findByRole('button', { name: /new cost centre/i }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: 'Code' }), 'CC-PROD')
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'Production again')
    await user.click(within(dialog).getByRole('combobox', { name: 'Type' }))
    await user.click(await screen.findByRole('option', { name: 'Service' }))
    await user.click(within(dialog).getByRole('button', { name: /create cost centre/i }))

    expect(
      await within(dialog).findByText('That code is already used in this company.'),
    ).toBeVisible()
  })
})

describe('editing and retiring a cost centre', () => {
  it('sends no code, and never offers a centre its own subtree as a parent', async () => {
    const { user, fetch } = renderScreen(<OrganisationPage />, {
      permissions: [P.COST_CENTRE_VIEW, P.COST_CENTRE_MANAGE],
      route: '/admin/organisation?tab=cost-centres',
      routes: {
        'GET /organization/cost-centres': costCentres([PRODUCTION, MACHINING]),
        'PATCH /organization/cost-centres/cc-mach': ok({ ...MACHINING, name: 'Machining shop' }),
      },
    })

    await user.click(await screen.findByRole('button', { name: 'Actions for CC-MACH' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    const dialog = await screen.findByRole('dialog')

    expect(within(dialog).getByRole('textbox', { name: 'Code' })).toBeDisabled()

    await user.click(within(dialog).getByRole('combobox', { name: 'Parent cost centre' }))
    expect(await screen.findByRole('option', { name: 'CC-PROD - Production' })).toBeVisible()
    // Itself and its own CNC cell: moving beneath either is a cycle.
    expect(screen.queryByRole('option', { name: /CC-MACH - Machining/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /CC-MACH-CNC/ })).not.toBeInTheDocument()
    await user.keyboard('{Escape}')

    const name = within(dialog).getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'Machining shop')
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('PATCH', '/organization/cost-centres/cc-mach'),
    )) as Record<string, unknown>
    expect(body).not.toHaveProperty('code')
    expect(body).toEqual({
      name: 'Machining shop',
      cost_centre_type: 'PRODUCTION',
      parent_id: 'cc-prod',
      plant_id: null,
      head_user_id: null,
      description: null,
      is_active: true,
    })
  })

  it('retires with the reason in the query string, and says what retiring refuses', async () => {
    let reason: string | null = null
    const { user } = renderScreen(<OrganisationPage />, {
      permissions: [P.COST_CENTRE_VIEW, P.COST_CENTRE_MANAGE],
      route: '/admin/organisation?tab=cost-centres',
      routes: {
        'GET /organization/cost-centres': costCentres([PRODUCTION, SALES]),
        'DELETE /organization/cost-centres/cc-sales': ({ url }) => {
          reason = url.searchParams.get('reason')
          return ok(null)
        },
      },
    })

    await user.click(await screen.findByRole('button', { name: 'Actions for CC-SALES' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Retire' }))

    const confirm = await screen.findByRole('dialog')
    expect(within(confirm).getByText(/deactivate it instead/i)).toBeVisible()

    await user.type(
      within(confirm).getByRole('textbox', { name: 'Reason' }),
      'Zone merged into West',
    )
    await user.click(within(confirm).getByRole('button', { name: 'Retire' }))

    await waitFor(() => expect(reason).toBe('Zone merged into West'))
  })
})

describe("a department's default cost centre", () => {
  const DEPARTMENT: DepartmentRow = {
    id: 'dept-buff',
    code: 'BUFF',
    name: 'Buffing shop',
    plant_id: null,
    parent_id: null,
    head_user_id: null,
    cost_centre_id: 'cc-mach',
    is_active: true,
    created_at: STAMP,
    updated_at: STAMP,
  }

  it('is named on the register and sent from the form to an account that may read centres', async () => {
    const { user, fetch } = renderScreen(<OrganisationPage />, {
      permissions: [P.DEPARTMENT_VIEW, P.DEPARTMENT_MANAGE, P.COST_CENTRE_VIEW],
      route: '/admin/organisation?tab=departments',
      routes: {
        'GET /organization/departments': page([DEPARTMENT]),
        'GET /organization/cost-centres': page(TREE),
        'GET /organization/plants': page([]),
        'PATCH /organization/departments/dept-buff': ok({ ...DEPARTMENT, cost_centre_id: null }),
      },
    })

    expect(await screen.findByRole('cell', { name: 'CC-MACH' })).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Actions for BUFF' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    const dialog = await screen.findByRole('dialog')

    await user.click(within(dialog).getByRole('combobox', { name: 'Default cost centre' }))
    await user.click(await screen.findByRole('option', { name: 'No default' }))
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('PATCH', '/organization/departments/dept-buff'),
    )) as Record<string, unknown>
    expect(body.cost_centre_id).toBeNull()
  })

  it('travels back unchanged from an account shown no picker', async () => {
    // Without `cost_centre.view` the default is neither shown nor asked for -
    // and an edit made by that account must not quietly clear it.
    const { user, fetch } = renderScreen(<OrganisationPage />, {
      permissions: [P.DEPARTMENT_VIEW, P.DEPARTMENT_MANAGE],
      route: '/admin/organisation?tab=departments',
      routes: {
        'GET /organization/departments': page([DEPARTMENT]),
        'GET /organization/plants': page([]),
        'PATCH /organization/departments/dept-buff': ok({ ...DEPARTMENT, name: 'Buffing' }),
      },
    })

    await user.click(await screen.findByRole('button', { name: 'Actions for BUFF' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    const dialog = await screen.findByRole('dialog')

    expect(
      within(dialog).queryByRole('combobox', { name: 'Default cost centre' }),
    ).not.toBeInTheDocument()

    const name = within(dialog).getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'Buffing')
    await user.click(within(dialog).getByRole('button', { name: /save changes/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('PATCH', '/organization/departments/dept-buff'),
    )) as Record<string, unknown>
    expect(body.cost_centre_id).toBe('cc-mach')
    expect(screen.queryByRole('columnheader', { name: 'Cost centre' })).not.toBeInTheDocument()
    expect(fetch.calls.some((call) => call.path.startsWith('/organization/cost-centres'))).toBe(
      false,
    )
  })
})

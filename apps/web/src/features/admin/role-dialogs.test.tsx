import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { ok, renderScreen } from '@/test/harness'

import { RoleFormDialog } from './role-dialogs'

/** `roles.name` is VARCHAR(120); the API refuses a longer name with a field error. */
const ROLE_NAME_MAX = 120

function renderRoleForm() {
  return renderScreen(<RoleFormDialog open onOpenChange={() => undefined} />, {
    permissions: [P.ROLE_MANAGE],
    routes: {
      'POST /roles': ok({ id: 'r-new', code: 'LONG_NAME', name: 'R'.repeat(ROLE_NAME_MAX) }),
    },
  })
}

describe('role form', () => {
  it('holds a role name to the width its column stores', async () => {
    const { user, fetch } = renderRoleForm()

    await user.type(screen.getByLabelText(/^code/i), 'LONG_NAME')
    const name = screen.getByLabelText(/^name/i)
    fireEvent.change(name, { target: { value: 'R'.repeat(ROLE_NAME_MAX + 1) } })
    await user.click(screen.getByRole('button', { name: 'Create role' }))

    expect(await screen.findByText(`At most ${ROLE_NAME_MAX} characters`)).toBeVisible()
    expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(0)

    fireEvent.change(name, { target: { value: 'R'.repeat(ROLE_NAME_MAX) } })
    await user.click(screen.getByRole('button', { name: 'Create role' }))

    await waitFor(() => {
      expect(fetch.calls.filter((call) => call.method === 'POST')).toHaveLength(1)
    })
    expect(fetch.calls.at(-1)?.body).toMatchObject({ name: 'R'.repeat(ROLE_NAME_MAX) })
  })
})

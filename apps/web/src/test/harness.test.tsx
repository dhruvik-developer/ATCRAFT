/**
 * The harness itself, tested.
 *
 * A test harness that quietly does nothing is worse than none: every screen
 * test built on it passes, and the suite reports coverage it does not have. So
 * this pins the three properties the rest of the suite relies on - that the real
 * auth provider really populates, that permissions really gate, and that an
 * unrouted request really fails rather than rendering an empty list.
 */

import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { useAuth } from '@/features/auth/auth-context'
import { api } from '@/lib/api-client'
import { P } from '@/lib/permissions'

import { fail, ok, page, renderScreen } from './harness'

function Probe() {
  const { user, can, isRestoring } = useAuth()
  if (isRestoring) return <p>restoring</p>
  return (
    <div>
      <p data-testid="email">{user?.email ?? 'anonymous'}</p>
      <p data-testid="may-view-items">{can(P.ITEM_VIEW) ? 'yes' : 'no'}</p>
      <p data-testid="may-delete-items">{can(P.ITEM_DELETE) ? 'yes' : 'no'}</p>
    </div>
  )
}

describe('renderScreen', () => {
  it('drives the real AuthProvider, so the signed-in user is genuinely restored', async () => {
    renderScreen(<Probe />, { permissions: [P.ITEM_VIEW] })
    await waitFor(() => expect(screen.getByTestId('email')).toHaveTextContent('tester@'))
  })

  it('gates on the permissions the test grants, and only those', async () => {
    renderScreen(<Probe />, { permissions: [P.ITEM_VIEW] })
    await waitFor(() => expect(screen.getByTestId('may-view-items')).toHaveTextContent('yes'))
    // The point of the harness: a right not granted is genuinely absent, so a
    // screen that gates on it renders the hidden branch.
    expect(screen.getByTestId('may-delete-items')).toHaveTextContent('no')
  })

  it('grants nothing by default, which is the honest starting position', async () => {
    renderScreen(<Probe />)
    await waitFor(() => expect(screen.getByTestId('email')).toHaveTextContent('tester@'))
    expect(screen.getByTestId('may-view-items')).toHaveTextContent('no')
  })
})

describe('the fetch boundary', () => {
  function Consumer({ path }: { path: string }) {
    const { isRestoring } = useAuth()
    void isRestoring
    return <button onClick={() => void api.get(path)}>go</button>
  }

  it('unwraps the envelope through the real api client', async () => {
    renderScreen(<Consumer path="/products/items/1" />, {
      routes: { 'GET /products/items': ok({ id: '1', code: 'BRASS-ROD' }) },
    })
    // Awaited here rather than fired through the button: the click handler
    // discards the promise with `void`, so an assertion after it runs before the
    // request settles and can only fail as an unhandled rejection - after the
    // test has already reported a pass. Awaiting the call is what actually
    // proves the two properties in this test's name: that the longest-prefix
    // match served the detail path from the list route, and that the client
    // returned the payload from inside the envelope rather than the envelope.
    const result = await api.get('/products/items/1')
    expect(result).toEqual({ id: '1', code: 'BRASS-ROD' })
  })

  it('records requests so a test can assert what a form actually sent', async () => {
    const { user, fetch } = renderScreen(<Consumer path="/products/items" />, {
      routes: { 'GET /products/items': page([]) },
    })
    await user.click(screen.getByRole('button', { name: 'go' }))
    await waitFor(() => fetch.requestFor('GET', '/products/items'))
  })

  it('refuses an unrouted request rather than answering it with nothing', async () => {
    // The property that stops a screen test passing while the screen silently
    // queries a path the test never anticipated: an empty list would look like
    // a correct render of no data.
    //
    // Awaited directly rather than through a click: the rejection happens after
    // the handler returns, so `user.click` resolves and the failure would escape
    // as an unhandled rejection instead of failing this test.
    renderScreen(<Consumer path="/products/items" />, {
      routes: { 'GET /products/items': page([]) },
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'go' })).toBeVisible())
    await expect(api.get('/somewhere/nobody/routed')).rejects.toThrow(/unrouted request/)
  })
})

describe('error bodies', () => {
  it('fail() carries the status the client should see', () => {
    const body = fail(422, 'VALIDATION_ERROR', 'Invalid', { fields: { code: ['taken'] } }) as {
      __status: number
      error: { code: string }
    }
    expect(body.__status).toBe(422)
    expect(body.error.code).toBe('VALIDATION_ERROR')
  })
})

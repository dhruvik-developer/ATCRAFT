/**
 * The upload dialog says a refusal once, inside the dialog.
 *
 * Its fields keep their own 422 detail - the file, the record, the retention -
 * and the alert carries the rest: the API's sentence for a refused file, or a
 * model-level 422 that belongs to no field. The mutation opts out of the global
 * toast, which never dismisses itself and would only repeat the alert.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import { P } from '@/lib/permissions'
import { fail, page, renderScreen, type RouteHandler } from '@/test/harness'

import DocumentsPage from './documents-page'

async function uploadRefusedWith(response: RouteHandler) {
  const view = renderScreen(
    <>
      <DocumentsPage />
      <Toaster />
    </>,
    {
      permissions: [P.DOCUMENT_VIEW, P.DOCUMENT_UPLOAD],
      errorNets: true,
      routes: { 'GET /documents': page([]), 'POST /documents': response },
    },
  )
  // The header offers it, and so does the empty list.
  const openers = await screen.findAllByRole('button', { name: 'Upload a file' })
  await view.user.click(openers[0] as HTMLElement)
  const dialog = await screen.findByRole('dialog')
  await view.user.upload(
    within(dialog).getByLabelText(/^File/),
    new File(['%PDF-1.7'], 'certificate.pdf', { type: 'application/pdf' }),
  )
  await view.user.click(within(dialog).getByRole('button', { name: 'Upload' }))
  await waitFor(() => expect(view.fetch.calls.some((call) => call.method === 'POST')).toBe(true))
  return dialog
}

function toastAlerts() {
  return within(screen.getByRole('region', { name: 'Notifications' })).queryAllByRole('alert')
}

describe('a refused upload', () => {
  it("shows the API's reason once, in the dialog", async () => {
    const dialog = await uploadRefusedWith(
      fail(415, 'UNSUPPORTED_MEDIA_TYPE', 'The file content does not match its declared type.'),
    )

    expect(await within(dialog).findByText('The file was not stored')).toBeVisible()
    expect(
      within(dialog).getByText('The file content does not match its declared type.'),
    ).toBeVisible()
    expect(toastAlerts()).toHaveLength(0)
  })

  it('names a model-level 422 in the dialog, where no field would', async () => {
    const dialog = await uploadRefusedWith(
      fail(422, 'VALIDATION_ERROR', 'Some fields need attention.', {
        fields: { _root: ['A document filed against a record needs both its type and its id'] },
      }),
    )

    expect(
      await within(dialog).findByText(
        'A document filed against a record needs both its type and its id',
      ),
    ).toBeVisible()
    expect(within(dialog).queryByText('Some fields need attention.')).not.toBeInTheDocument()
    expect(toastAlerts()).toHaveLength(0)
  })
})

/**
 * The upload dialog says a refusal once, inside the dialog.
 *
 * Its fields keep their own 422 detail - the file, the record, the retention -
 * and the alert carries the rest: the API's sentence for a refused file, or a
 * model-level 422 that belongs to no field. The mutation opts out of the global
 * toast, which never dismisses itself and would only repeat the alert.
 */

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { Toaster } from '@/components/ui/toaster'
import { P } from '@/lib/permissions'
import { fail, page, renderScreen, type RouteHandler } from '@/test/harness'

import { KNOWN_ENTITY_TYPES } from './document-model'
import DocumentsPage from './documents-page'

async function uploadRefusedWith(response: RouteHandler, record?: { type: string; id: string }) {
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
  if (record) {
    await view.user.type(within(dialog).getByLabelText('Attach to record type'), record.type)
    await view.user.type(within(dialog).getByLabelText('Record id'), record.id)
  }
  await view.user.click(within(dialog).getByRole('button', { name: 'Upload' }))
  await waitFor(() => expect(view.fetch.calls.some((call) => call.method === 'POST')).toBe(true))
  return dialog
}

function toastAlerts() {
  return within(screen.getByRole('region', { name: 'Notifications' })).queryAllByRole('alert')
}

describe('who can read what is uploaded', () => {
  async function openUpload() {
    const view = renderScreen(<DocumentsPage />, {
      permissions: [P.DOCUMENT_VIEW, P.DOCUMENT_UPLOAD],
      routes: { 'GET /documents': page([]) },
    })
    const openers = await screen.findAllByRole('button', { name: 'Upload a file' })
    await view.user.click(openers[0] as HTMLElement)
    return { ...view, dialog: await screen.findByRole('dialog') }
  }

  it('says a file is for signed-in accounts when it is filed against no item', async () => {
    const { user, dialog } = await openUpload()
    await user.type(within(dialog).getByLabelText('Attach to record type'), 'qc_inspection')

    expect(dialog).toHaveAccessibleDescription(
      /downloadable only by someone signed in to it\. The server checks the content/,
    )
    expect(within(dialog).queryByText(/dealer catalogue/)).not.toBeInTheDocument()
  })

  it('says an image filed against an item is published to every dealer', async () => {
    // The dealer catalogue shows every JPEG, PNG or WebP filed against a
    // sellable item to every dealer, by the record type alone (§DOC-1), so
    // "only someone signed in" misled the person publishing it.
    const { user, dialog } = await openUpload()
    await user.type(within(dialog).getByLabelText('Attach to record type'), ' Item ')

    expect(dialog).toHaveAccessibleDescription(
      /appears in the dealer catalogue, where every dealer can see and download it/,
    )
    expect(within(dialog).queryByText(/downloadable only by someone/)).not.toBeInTheDocument()
  })
})

describe('a refused upload', () => {
  it("shows the API's reason once, in the dialog", async () => {
    const dialog = await uploadRefusedWith(
      fail(415, 'UNSUPPORTED_MEDIA_TYPE', 'The file content does not match its declared type.'),
    )

    expect(await within(dialog).findByText('Could not store the file')).toBeVisible()
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

describe('the record an upload is attached to', () => {
  it('offers every record type the API resolves, with a name a person would use', async () => {
    renderScreen(<DocumentsPage />, {
      permissions: [P.DOCUMENT_VIEW, P.DOCUMENT_UPLOAD],
      routes: { 'GET /documents': page([]) },
    })
    const openers = await screen.findAllByRole('button', { name: 'Upload a file' })
    await userEvent.click(openers[0] as HTMLElement)
    await screen.findByRole('dialog')

    const options = Array.from(
      document.querySelectorAll<HTMLOptionElement>('#document-entity-type-options option'),
    )
    expect(options.map((option) => option.value)).toEqual([...KNOWN_ENTITY_TYPES])
    expect(options.map((option) => option.value)).toContain('purchase_order')
    expect(options.find((option) => option.value === 'goods_receipt')?.label).toBe('Goods receipt')
    expect(options.every((option) => option.label !== '')).toBe(true)
  })

  it("puts the API's 404 on the record id field, once", async () => {
    const dialog = await uploadRefusedWith(
      fail(404, 'NOT_FOUND', 'Purchase order 3f2a not found.'),
      { type: 'purchase_order', id: '0197c2a4-5b1e-7c3d-8a9b-0123456789ab' },
    )

    const message = await within(dialog).findByText('Purchase order 3f2a not found.')
    expect(message).toBeVisible()
    expect(within(dialog).getByLabelText('Record id')).toHaveAccessibleDescription(
      'Purchase order 3f2a not found.',
    )
    expect(within(dialog).queryByText('Could not store the file')).not.toBeInTheDocument()
    expect(toastAlerts()).toHaveLength(0)
  })
})

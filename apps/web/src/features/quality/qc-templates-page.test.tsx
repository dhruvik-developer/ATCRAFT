/**
 * Inspection specifications - the check sheets quality measures against.
 *
 * Two properties of this screen decide whether the record stays reproducible,
 * and neither is visible to the compiler.
 *
 * **Writing a specification is a different right from applying one.**
 * `qc.template_manage` is separate from `qc.inspect` and `qc.decide` because the
 * person who measures a part is not the person authorised to write the standard
 * it is measured against (master rules 38). Every write control on this screen
 * is gated on it, and a gate on the wrong right here means an inspector can
 * quietly move the tolerance band their own lot is judged by.
 *
 * **An amend sends only what changed.** `update_template` decides whether an
 * edit "restates the specification" from the keys *present* in the payload, not
 * from whether their values differ - so a form that posts its whole state turns
 * a typo fix into a specification change and is refused on every template that
 * has ever been inspected against. That is the single most expensive mistake
 * this screen can make, and it is invisible until a used template is edited.
 */

import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { P } from '@/lib/permissions'
import { fail, ok, page, renderScreen } from '@/test/harness'

import QcTemplatesPage from './qc-templates-page'
import type { QcTemplateRow } from './qc-template-model'

const TEMPLATE_ID = '01a00000-0000-7000-8000-0000000000t1'

/** One row of `GET /quality/templates` (`TemplateSummary`). */
function template(overrides: Partial<QcTemplateRow> = {}): QcTemplateRow {
  return {
    id: TEMPLATE_ID,
    code: 'QC-BRASS-ROD',
    version: 1,
    name: 'Brass rod incoming check',
    qc_stage: 'INCOMING',
    item_id: null,
    item_category_id: null,
    sampling_plan: 'ALL',
    sample_percentage: null,
    sample_size: null,
    is_active: true,
    created_at: '2026-08-01T08:00:00Z',
    updated_at: '2026-08-20T08:00:00Z',
    ...overrides,
  }
}

/** `GET /quality/templates/{id}` - the summary plus its check sheet. */
function templateDetail(overrides: Record<string, unknown> = {}) {
  return {
    ...template(),
    description: 'Every rod, every consignment.',
    parameters: [
      {
        id: '01a00000-0000-7000-8000-0000000000d1',
        template_id: TEMPLATE_ID,
        sequence: 1,
        parameter_name: 'Overall length',
        parameter_type: 'NUMERIC',
        unit: 'mm',
        min_value: '12.5000',
        max_value: '13.5000',
        target_value: null,
        allowed_values: null,
        is_critical: false,
        is_mandatory: true,
      },
    ],
    ...overrides,
  }
}

/** Records the query string of each request, which `fetch.calls` drops. */
function urlRecorder() {
  const urls: URL[] = []
  return {
    urls,
    paramsAt(index: number): Record<string, string> {
      const url = urls[index]
      expect(url, `expected at least ${index + 1} request(s)`).toBeDefined()
      return Object.fromEntries(url!.searchParams)
    },
  }
}

describe('who may write a specification', () => {
  it('offers the create and per-row write actions to a template manager', async () => {
    renderScreen(<QcTemplatesPage />, {
      permissions: [P.QC_VIEW, P.QC_TEMPLATE_MANAGE],
      routes: { 'GET /quality/templates': page([template()]) },
    })

    expect(await screen.findByRole('button', { name: /new specification/i })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Amend QC-BRASS-ROD v1' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'More actions for QC-BRASS-ROD v1' })).toBeVisible()
  })

  it('withholds every write control from a reader', async () => {
    // `qc.view` reads the specifications an inspector is judged against. It must
    // not also let them move the tolerance band that judges their own lot.
    renderScreen(<QcTemplatesPage />, {
      permissions: [P.QC_VIEW],
      routes: { 'GET /quality/templates': page([template()]) },
    })

    expect(await screen.findByText('QC-BRASS-ROD')).toBeVisible()
    expect(screen.queryByRole('button', { name: /new specification/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^amend/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /more actions/i })).toBeNull()
  })

  it('does not read the category catalogue without the right to see it', async () => {
    // The filter is not merely hidden: an unrouted request would throw here, so
    // this also pins that the screen does not fire a query it cannot use. A
    // 403 on a reference read is a toast the user can do nothing about.
    renderScreen(<QcTemplatesPage />, {
      permissions: [P.QC_VIEW],
      routes: { 'GET /quality/templates': page([template()]) },
    })

    expect(await screen.findByText('QC-BRASS-ROD')).toBeVisible()
    expect(screen.queryByRole('combobox', { name: /filter by item category/i })).toBeNull()
  })

  it('reads the category catalogue when the account may see it', async () => {
    renderScreen(<QcTemplatesPage />, {
      permissions: [P.QC_VIEW, P.CATEGORY_VIEW],
      routes: {
        'GET /quality/templates': page([template()]),
        'GET /products/categories': page([
          { id: 'c1', code: 'RAW', name: 'Raw material', is_active: true },
        ]),
      },
    })

    expect(await screen.findByRole('combobox', { name: /filter by item category/i })).toBeVisible()
  })
})

describe('what the list asks for', () => {
  it('asks for the versions in force by default, not the whole history', async () => {
    // Every row is a version, and most of them are superseded. Dropping the
    // default would bury the specification actually in force under its own
    // history - three quarters of the working list would be dead versions.
    const recorder = urlRecorder()
    renderScreen(<QcTemplatesPage />, {
      permissions: [P.QC_VIEW],
      routes: {
        'GET /quality/templates': ({ url }) => {
          recorder.urls.push(url)
          return page([template()])
        },
      },
    })

    expect(await screen.findByText('QC-BRASS-ROD')).toBeVisible()
    expect(recorder.paramsAt(0).is_active).toBe('true')
  })

  it('drops the flag entirely when every version is asked for', async () => {
    // `is_active=any` would be a 422 against a boolean query parameter, and
    // `is_active=false` is a different question - the superseded ones only.
    const recorder = urlRecorder()
    renderScreen(<QcTemplatesPage />, {
      permissions: [P.QC_VIEW],
      route: '/quality/templates?active=any',
      routes: {
        'GET /quality/templates': ({ url }) => {
          recorder.urls.push(url)
          return page([template()])
        },
      },
    })

    expect(await screen.findByText('QC-BRASS-ROD')).toBeVisible()
    expect(recorder.paramsAt(0).is_active).toBeUndefined()
    expect(recorder.paramsAt(0).active).toBeUndefined()
  })

  it('forwards the scope filters under the names the API answers', async () => {
    const recorder = urlRecorder()
    renderScreen(<QcTemplatesPage />, {
      permissions: [P.QC_VIEW],
      route: '/quality/templates?qc_stage=FINAL&sampling_plan=AQL&active=false',
      routes: {
        'GET /quality/templates': ({ url }) => {
          recorder.urls.push(url)
          return page([template({ qc_stage: 'FINAL', sampling_plan: 'AQL', is_active: false })])
        },
      },
    })

    expect(await screen.findByText('QC-BRASS-ROD')).toBeVisible()
    const params = recorder.paramsAt(0)
    expect(params.qc_stage).toBe('FINAL')
    expect(params.sampling_plan).toBe('AQL')
    expect(params.is_active).toBe('false')
  })

  it('tells a reader with no specifications what the absence means', async () => {
    renderScreen(<QcTemplatesPage />, {
      permissions: [P.QC_VIEW],
      routes: { 'GET /quality/templates': page([]) },
    })

    expect(await screen.findByText('No specification is in force yet')).toBeVisible()
  })

  it('distinguishes "nothing matches" from "nothing exists"', async () => {
    // The two say opposite things to an inspector looking for a check sheet:
    // one means widen the filters, the other means write one.
    renderScreen(<QcTemplatesPage />, {
      permissions: [P.QC_VIEW],
      route: '/quality/templates?qc_stage=FINAL',
      routes: { 'GET /quality/templates': page([]) },
    })

    expect(await screen.findByText('No specification matches')).toBeVisible()
  })

  it('calls a superseded version superseded rather than inactive', async () => {
    // "Superseded" and "retired" are different states - a retired specification
    // is soft-deleted and gone from every list - and the copy keeps them apart.
    renderScreen(<QcTemplatesPage />, {
      permissions: [P.QC_VIEW],
      routes: {
        'GET /quality/templates': page([
          template({ is_active: true }),
          template({ id: 'v2', code: 'QC-OLD', version: 2, is_active: false }),
        ]),
      },
    })

    const row = (await screen.findByText('QC-OLD')).closest('tr')
    expect(row).not.toBeNull()
    expect(within(row!).getByText('Superseded')).toBeVisible()
  })
})

describe('amending a version', () => {
  /** Open the amend dialog from the row action, the way the screen reaches it. */
  async function openAmend(routes: Record<string, unknown> = {}) {
    const rendered = renderScreen(<QcTemplatesPage />, {
      permissions: [P.QC_VIEW, P.QC_TEMPLATE_MANAGE],
      routes: {
        'GET /quality/templates': page([template()]),
        [`GET /quality/templates/${TEMPLATE_ID}`]: ok(templateDetail()),
        ...routes,
      },
    })
    await rendered.user.click(await screen.findByRole('button', { name: 'Amend QC-BRASS-ROD v1' }))
    await screen.findByDisplayValue('Brass rod incoming check')
    return rendered
  }

  it('sends only the field that changed, and never the untouched check sheet', async () => {
    const { user, fetch } = await openAmend({
      [`PATCH /quality/templates/${TEMPLATE_ID}`]: ok(templateDetail({ name: 'Brass rod check' })),
    })

    const name = screen.getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'Brass rod check')
    await user.click(screen.getByRole('button', { name: /save details/i }))

    const body = (await waitFor(() =>
      fetch.requestFor('PATCH', `/quality/templates/${TEMPLATE_ID}`),
    )) as Record<string, unknown>

    expect(body).toEqual({ name: 'Brass rod check' })
    // The key that must not be there. `update_template` reads the *presence* of
    // `parameters`, so re-sending the sheet unchanged reclassifies a rename as a
    // specification change and gets it refused on any template ever applied.
    expect(body).not.toHaveProperty('parameters')
    expect(body).not.toHaveProperty('qc_stage')
    expect(body).not.toHaveProperty('sampling_plan')
  })

  it('will not submit an amend that changes nothing', async () => {
    // A PATCH with an empty body still counts as an edit in the audit trail,
    // and on a used template it is a refusal for a change nobody made.
    const { user, fetch } = await openAmend()

    expect(screen.getByRole('button', { name: /save details/i })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: /cancel/i }))
    expect(fetch.calls.some((call) => call.method === 'PATCH')).toBe(false)
  })

  it('keeps the code out of the payload, because a version cannot be renamed', async () => {
    // The code identifies the specification across every one of its versions and
    // `TemplateUpdate` has no field for it, so the control is read-only.
    await openAmend()

    expect(screen.getByRole('textbox', { name: 'Code' })).toHaveAttribute('readonly')
  })

  it('offers the fork when the API refuses the edit as frozen', async () => {
    // The refusal is not the end of the road: the whole point of versioning is
    // that a used specification is forked rather than rewritten, so the dialog
    // says which of the two acts is still available.
    const { user } = await openAmend({
      [`PATCH /quality/templates/${TEMPLATE_ID}`]: fail(
        409,
        'REFERENCED_RECORD_IN_USE',
        'This version has already been inspected against',
      ),
    })

    const name = screen.getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'Brass rod check')
    await user.click(screen.getByRole('button', { name: /save details/i }))

    expect(await screen.findByText('This version is frozen')).toBeVisible()
    expect(await screen.findByText('This version has already been inspected against')).toBeVisible()
  })

  it('puts a field-level refusal on the field it belongs to', async () => {
    const { user } = await openAmend({
      [`PATCH /quality/templates/${TEMPLATE_ID}`]: fail(422, 'VALIDATION_ERROR', 'Invalid', {
        fields: { name: ['Another specification already has this name'] },
      }),
    })

    const name = screen.getByRole('textbox', { name: 'Name' })
    await user.clear(name)
    await user.type(name, 'Brass rod check')
    await user.click(screen.getByRole('button', { name: /save details/i }))

    // The query client stays silent for a 422 carrying field errors precisely
    // because the form is supposed to show them; if it does not, the save looks
    // to the user like it simply did nothing.
    expect(await screen.findByText('Another specification already has this name')).toBeVisible()
  })
})

describe('creating a specification', () => {
  async function openCreate(routes: Record<string, unknown> = {}) {
    const rendered = renderScreen(<QcTemplatesPage />, {
      permissions: [P.QC_VIEW, P.QC_TEMPLATE_MANAGE],
      routes: { 'GET /quality/templates': page([]), ...routes },
    })
    // The empty state repeats the header's action, so there are two of these
    // buttons on an empty list; the header one is the first in the document.
    const [create] = await screen.findAllByRole('button', { name: /new specification/i })
    await rendered.user.click(create!)
    return rendered
  }

  it('refuses to post an item-scoped specification with no item', async () => {
    // The scope decides which incoming lot resolves to this sheet. An item
    // scope with no item is not a narrower specification, it is one that
    // resolves to nothing.
    const { user, fetch } = await openCreate()

    await user.type(screen.getByRole('textbox', { name: 'Code' }), 'QC-NEW')
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'New check')
    await user.type(
      screen.getByRole('textbox', { name: 'Name of characteristic 1' }),
      'Overall length',
    )
    await user.click(screen.getByRole('button', { name: /create specification/i }))

    expect(await screen.findByText('Choose the item this specification governs')).toBeVisible()
    expect(fetch.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('posts a stage-wide specification with the figures its plan does not use cleared', async () => {
    const { user, fetch } = await openCreate({
      'POST /quality/templates': ok(templateDetail({ code: 'QC-NEW' })),
    })

    await user.type(screen.getByRole('textbox', { name: 'Code' }), 'QC-NEW')
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'New check')

    // Radix's Select is a listbox, not a <select>, so the scope is chosen the
    // way a user does it.
    await user.click(screen.getByRole('combobox', { name: /applies to/i }))
    await user.click(await screen.findByRole('option', { name: 'Stage default' }))

    await user.type(
      screen.getByRole('textbox', { name: 'Name of characteristic 1' }),
      'Overall length',
    )
    await user.type(screen.getByRole('textbox', { name: 'Minimum' }), '12.5')
    await user.type(screen.getByRole('textbox', { name: 'Maximum' }), '13.5')
    await user.click(screen.getByRole('button', { name: /create specification/i }))

    const body = (await waitFor(() => fetch.requestFor('POST', '/quality/templates'))) as Record<
      string,
      unknown
    >

    expect(body.code).toBe('QC-NEW')
    // Both scope columns null is the stage default, and they are mutually
    // exclusive on the wire - sending the empty item id as '' would claim an
    // item scope that names no item.
    expect(body.item_id).toBeNull()
    expect(body.item_category_id).toBeNull()
    // `TemplateCreate` refuses a figure that does not belong to the plan, so
    // the ones the ALL plan does not use are cleared rather than sent blank.
    expect(body.sample_percentage).toBeNull()
    expect(body.sample_size).toBeNull()
    expect(body.parameters).toEqual([
      {
        sequence: 1,
        parameter_name: 'Overall length',
        parameter_type: 'NUMERIC',
        unit: null,
        // Limits stay decimal strings: a 12.5 band read back through a JS
        // number rejects a reading that is exactly on the limit.
        min_value: '12.5',
        max_value: '13.5',
        target_value: null,
        allowed_values: null,
        is_critical: false,
        is_mandatory: true,
      },
    ])
  })
})

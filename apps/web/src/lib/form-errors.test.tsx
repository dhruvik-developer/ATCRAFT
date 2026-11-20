import { act, render, renderHook, screen, waitFor } from '@testing-library/react'
import { Controller, useForm, type UseFormSetError } from 'react-hook-form'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { FieldError } from '@/components/field-error'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { CostCentreSelect } from '@/features/admin/cost-centre-select'
import { renderScreen } from '@/test/harness'

import { ApiError } from './api-client'
import {
  applyFieldErrors,
  fieldErrorSummary,
  fieldLabel,
  partitionFieldErrors,
  remapArrayIndices,
  reportFieldErrors,
} from './form-errors'
import { toast } from './toast'

afterEach(() => {
  vi.restoreAllMocks()
})

/** A 422 as the API shapes it: `details.fields`, keyed by dotted path. */
function validationError(fields: Record<string, string[]>): ApiError {
  return new ApiError(422, 'VALIDATION_ERROR', 'Invalid request', { fields })
}

describe('form error paths', () => {
  it('maps a sparse payload index back to the row shown in the form', () => {
    const map = remapArrayIndices('lines', [1, 4, 8])

    expect(map('lines.0.quantity')).toBe('lines.1.quantity')
    expect(map('lines.2')).toBe('lines.8')
    expect(map('lines.invalid.quantity')).toBe('lines.invalid.quantity')
    expect(map('header.reference')).toBe('header.reference')
  })

  it('separates model and query errors from errors owned by controls', () => {
    const error = validationError({
      'lines.0.quantity': ['Must be positive'],
      _root: ['At least one line is required'],
      'query.page': ['Must be an integer'],
    })

    expect(partitionFieldErrors(error)).toEqual({
      fields: { 'lines.0.quantity': ['Must be positive'] },
      orphans: ['At least one line is required', 'Must be an integer'],
    })
  })

  it('treats bare and nested transport parameters as orphan errors', () => {
    const error = validationError({
      query: ['Choose a bounded date range.'],
      'path.id': ['Enter a valid identifier.'],
      header: ['A required header is missing.'],
      'cookie.session': ['Sign in again.'],
      quantity: ['Enter a positive quantity.'],
    })
    expect(partitionFieldErrors(error)).toEqual({
      fields: { quantity: ['Enter a positive quantity.'] },
      orphans: [
        'Choose a bounded date range.',
        'Enter a valid identifier.',
        'A required header is missing.',
        'Sign in again.',
      ],
    })
  })

  it('ignores empty field messages so they cannot silence the global error notice', () => {
    expect(partitionFieldErrors(validationError({ quantity: [], _root: ['  '] }))).toEqual({
      fields: {},
      orphans: [],
    })
  })
})

describe('applyFieldErrors', () => {
  it('addresses a field array entry by the dotted path the API sends', () => {
    const setError = vi.fn()

    applyFieldErrors(
      validationError({ 'lines.0.scrap_percentage': ['Must be at most 100'] }),
      setError,
    )

    expect(setError).toHaveBeenCalledTimes(1)
    expect(setError).toHaveBeenCalledWith(
      'lines.0.scrap_percentage',
      { type: 'server', message: 'Must be at most 100' },
      { shouldFocus: true },
    )
  })

  it('puts an error on a whole array entry when the path stops at the index', () => {
    const setError = vi.fn()

    applyFieldErrors(
      validationError({ 'operations.0': ['Sequence numbers must be unique'] }),
      setError,
    )

    expect(setError).toHaveBeenCalledWith(
      'operations.0',
      { type: 'server', message: 'Sequence numbers must be unique' },
      { shouldFocus: true },
    )
  })

  it('joins several messages on one control with a full stop', () => {
    const setError = vi.fn()

    applyFieldErrors(validationError({ code: ['Required', 'Must be unique'] }), setError)

    expect(setError).toHaveBeenCalledWith(
      'code',
      { type: 'server', message: 'Required. Must be unique' },
      { shouldFocus: true },
    )
  })

  it('preserves punctuation when the API already sends complete sentences', () => {
    const setError = vi.fn()
    applyFieldErrors(
      validationError({ code: ['Code is required.', 'Choose a unique code.'] }),
      setError,
    )
    expect(setError).toHaveBeenCalledWith(
      'code',
      { type: 'server', message: 'Code is required. Choose a unique code.' },
      { shouldFocus: true },
    )
  })

  it('lands a model-level refusal and a query error on root, joined', () => {
    const setError = vi.fn()

    applyFieldErrors(
      validationError({
        _root: ['At least one line is required'],
        'query.page': ['Must be an integer'],
      }),
      setError,
    )

    expect(setError).toHaveBeenCalledTimes(1)
    expect(setError).toHaveBeenCalledWith('root', {
      type: 'server',
      message: 'At least one line is required. Must be an integer',
    })
  })

  it('rewrites the path through mapPath before looking the control up', () => {
    const setError = vi.fn()

    applyFieldErrors(
      validationError({ 'lines.0.quantity': ['Must be positive'] }),
      setError,
      remapArrayIndices('lines', [3]),
    )

    expect(setError).toHaveBeenCalledWith(
      'lines.3.quantity',
      { type: 'server', message: 'Must be positive' },
      { shouldFocus: true },
    )
  })

  it('ignores anything that is not an ApiError', () => {
    const setError = vi.fn()

    applyFieldErrors(new Error('network'), setError)

    expect(setError).not.toHaveBeenCalled()
  })

  it('stops asking for the focus once a refused control has it, and never focuses the root', () => {
    const input = document.createElement('input')
    document.body.append(input)
    // What react-hook-form does for a field whose registration holds an element.
    const setError = vi.fn((name: string, _error: unknown, options?: { shouldFocus?: boolean }) => {
      if (name === 'employee_code' && options?.shouldFocus) input.focus()
    })

    applyFieldErrors(
      validationError({
        employee_code: ['Already in use'],
        phone: ['Not a phone number'],
        _root: ['Check the form'],
      }),
      setError,
    )

    expect(setError.mock.calls).toEqual([
      ['employee_code', { type: 'server', message: 'Already in use' }, { shouldFocus: true }],
      ['phone', { type: 'server', message: 'Not a phone number' }, { shouldFocus: false }],
      ['root', { type: 'server', message: 'Check the form' }],
    ])
    input.remove()
  })

  it('asks every refused control in turn while none of them takes the focus', () => {
    const setError = vi.fn()

    applyFieldErrors(
      validationError({ employee_code: ['Already in use'], phone: ['Not a phone number'] }),
      setError,
    )

    expect(setError.mock.calls).toEqual([
      ['employee_code', { type: 'server', message: 'Already in use' }, { shouldFocus: true }],
      ['phone', { type: 'server', message: 'Not a phone number' }, { shouldFocus: true }],
    ])
  })

  it('moves the focus to that control on a real form', () => {
    const { result } = renderHook(() => useForm<{ code: string; name: string }>())
    const inputs = ['code', 'name'].map((name) => {
      const input = document.createElement('input')
      document.body.append(input)
      result.current.register(name as 'code' | 'name').ref(input)
      return input
    })

    act(() =>
      applyFieldErrors(
        validationError({ name: ['Required'], code: ['Already in use'] }),
        result.current.setError,
      ),
    )

    expect(document.activeElement).toBe(inputs[1])
    for (const input of inputs) input.remove()
  })
})

/**
 * Where the focus goes on a real form, laid out the way the screens lay one out:
 * a label, the control and the `FieldError` under it.
 *
 * react-hook-form focuses a refused field only through the element its
 * registration holds. A `Controller` whose render does not hand `field.ref` to
 * an element holds none - 233 of the 234 in this app did not - so asking it for
 * the focus did nothing, and only the first refused field was ever asked: the
 * message sat under a select scrolled out of sight, as before the focus existed.
 */
describe('applyFieldErrors on a rendered form', () => {
  interface Values {
    cost_centre_id: string
    code: string
  }

  const CENTRE_REFUSED = 'Choose an active cost centre.'
  const CODE_REFUSED = 'Choose a code not already in use.'

  /** The cost centre is a Controller-driven Radix select that never receives `field.ref`. */
  function RefusableForm({
    form,
    codeDisabled = false,
  }: {
    form: { setError?: UseFormSetError<Values> }
    codeDisabled?: boolean
  }) {
    const {
      control,
      register,
      setError,
      formState: { errors },
    } = useForm<Values>({ defaultValues: { cost_centre_id: '', code: '' } })
    form.setError = setError
    return (
      <form>
        <div>
          <label htmlFor="cost-centre">Cost centre</label>
          <Controller
            control={control}
            name="cost_centre_id"
            render={({ field }) => (
              <Select value={field.value} onValueChange={field.onChange}>
                <SelectTrigger id="cost-centre">
                  <SelectValue placeholder="Unallocated" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="cc-plate">CC-PLATE - Plating</SelectItem>
                </SelectContent>
              </Select>
            )}
          />
          <FieldError message={errors.cost_centre_id?.message} />
        </div>
        <div>
          <label htmlFor="code">Code</label>
          <input id="code" disabled={codeDisabled} {...register('code')} />
          <FieldError message={errors.code?.message} />
        </div>
        <button type="submit">Save</button>
      </form>
    )
  }

  function refuse(form: { setError?: UseFormSetError<Values> }, fields: Record<string, string[]>) {
    act(() => applyFieldErrors(validationError(fields), form.setError!))
  }

  it('focuses a select whose Controller holds no element, once its error has rendered', async () => {
    const form: { setError?: UseFormSetError<Values> } = {}
    render(<RefusableForm form={form} />)
    const save = screen.getByRole('button', { name: 'Save' })
    save.focus()

    refuse(form, { cost_centre_id: [CENTRE_REFUSED] })

    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Cost centre' })).toHaveFocus())
    expect(screen.getByText(CENTRE_REFUSED)).toBeVisible()
  })

  it('asks the next refused field when the first cannot take the focus', () => {
    const form: { setError?: UseFormSetError<Values> } = {}
    render(<RefusableForm form={form} />)
    screen.getByRole('button', { name: 'Save' }).focus()

    refuse(form, { cost_centre_id: [CENTRE_REFUSED], code: [CODE_REFUSED] })

    expect(screen.getByRole('textbox', { name: 'Code' })).toHaveFocus()
  })

  it('brings the error into view when no refused control can take the focus', async () => {
    const scroll = vi.spyOn(Element.prototype, 'scrollIntoView')
    const form: { setError?: UseFormSetError<Values> } = {}
    render(<RefusableForm form={form} codeDisabled />)
    const save = screen.getByRole('button', { name: 'Save' })
    save.focus()

    refuse(form, { code: [CODE_REFUSED] })

    const message = screen.getByText(CODE_REFUSED)
    await waitFor(() => expect(scroll.mock.contexts).toContain(message))
    expect(scroll).toHaveBeenCalledWith({ block: 'center' })
    expect(save).toHaveFocus()
  })

  it('focuses a cost-centre picker through the ref its Controller hands it', () => {
    const form: { setError?: UseFormSetError<Values> } = {}
    function CostCentreForm() {
      const {
        control,
        setError,
        formState: { errors },
      } = useForm<Values>({ defaultValues: { cost_centre_id: '', code: '' } })
      form.setError = setError
      return (
        <Controller
          control={control}
          name="cost_centre_id"
          render={({ field }) => (
            <CostCentreSelect
              ref={field.ref}
              id="cost-centre"
              value={field.value || null}
              onChange={(next) => field.onChange(next ?? '')}
              enabled={false}
              error={errors.cost_centre_id?.message}
            />
          )}
        />
      )
    }
    renderScreen(<CostCentreForm />)

    refuse(form, { cost_centre_id: [CENTRE_REFUSED] })

    // At once, through react-hook-form - not a frame later, through the error.
    expect(screen.getByRole('combobox', { name: 'Cost centre' })).toHaveFocus()
  })
})

describe('fieldErrorSummary', () => {
  it('joins every message, for a control that is not a form field', () => {
    expect(
      fieldErrorSummary(validationError({ reason: ['Too short'], _root: ['Not allowed'] })),
    ).toBe('Too short. Not allowed')
  })

  it('is null when there is nothing field-level to show', () => {
    expect(fieldErrorSummary(new ApiError(409, 'CONFLICT', 'Changed by someone else'))).toBeNull()
    expect(fieldErrorSummary(new Error('network'))).toBeNull()
  })
})

describe('fieldLabel', () => {
  it('numbers a line from one, in place of its collection', () => {
    expect(fieldLabel('lines.0.quantity')).toBe('Line 1 quantity')
    expect(fieldLabel('lines.4')).toBe('Line 5')
    expect(fieldLabel('lines')).toBe('Lines')
  })

  it('puts a snake_case key into words', () => {
    expect(fieldLabel('promised_date')).toBe('Promised date')
    expect(fieldLabel('rules.2.threshold_days')).toBe('Rule 3 threshold days')
  })

  it('names the item of any collection in the singular', () => {
    expect(fieldLabel('entries.0.amount')).toBe('Entry 1 amount')
    expect(fieldLabel('batches.1.batch_id')).toBe('Batch 2 batch id')
    expect(fieldLabel('addresses.0.city')).toBe('Address 1 city')
    expect(fieldLabel('suggestion_ids.3')).toBe('Suggestion id 4')
  })

  it('numbers a bare index as a line', () => {
    expect(fieldLabel('0.quantity')).toBe('Line 1 quantity')
  })
})

describe('reportFieldErrors', () => {
  const toastError = () => vi.spyOn(toast, 'error').mockImplementation(() => '')

  it('names each field in words, numbered from one, joined with a dash', () => {
    const shown = toastError()
    reportFieldErrors(
      new ApiError(422, 'VALIDATION_ERROR', 'Some fields need attention.', {
        fields: {
          'lines.0.quantity': ['Input should be greater than 0'],
          promised_date: ['Input should be a valid date'],
        },
      }),
      'Could not save these lines',
    )
    expect(shown).toHaveBeenCalledOnce()
    expect(shown).toHaveBeenCalledWith(
      'Could not save these lines',
      'Line 1 quantity: Input should be greater than 0 - Promised date: Input should be a valid date',
    )
  })

  it('says nothing the global net already says', () => {
    const shown = toastError()
    reportFieldErrors(new TypeError('boom'), 'Could not save')
    reportFieldErrors(new ApiError(409, 'CONFLICT', 'Changed by someone else.'), 'Could not save')
    // Orphans are the net's: it toasts a model-level or query refusal itself.
    reportFieldErrors(
      new ApiError(422, 'VALIDATION_ERROR', 'Some fields need attention.', {
        fields: { _root: ['The order is closed'], 'query.reason': ['Too short'] },
      }),
      'Could not save',
    )
    expect(shown).not.toHaveBeenCalled()
  })
})

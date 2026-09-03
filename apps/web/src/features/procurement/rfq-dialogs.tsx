/**
 * The two dialogs an RFQ needs: the form that raises or edits one, and the
 * confirmation that carries out a lifecycle command.
 *
 * Both are shared by the list and the detail screen, so a buyer clearing a
 * queue of drafts and a buyer reading one enquiry get the same wording, the
 * same validation and the same permission gate.
 *
 * The form is the interesting one, for three reasons the contract imposes.
 *
 * **A vendor does not have to be approved to be asked for a price.** Unlike a
 * purchase order - where the API refuses an unapproved supplier outright - an
 * RFQ only checks that the vendors exist. Asking for a quotation is often how
 * the evidence to approve a vendor gets gathered, so the picker here is *not*
 * restricted the way the purchase order form's is.
 *
 * **An edit replaces the whole line set.** `PATCH /procurement/rfqs/{id}`
 * deletes every line and rebuilds it from the payload, so any field this form
 * does not send is lost - including the ones it never shows. The description,
 * unit, note and requisition link on each line are therefore carried through
 * the form untouched, and cleared only when the item on that line changes.
 *
 * **Editing stops at DRAFT.** Vendors quote against the lines as they were
 * issued, so the API refuses an edit once the RFQ has been sent. The dialog is
 * never opened for one - the screens do not offer it - and it says so if it
 * somehow is.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, ChevronsUpDown, Loader2, Plus, Search, Trash2, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Controller, useFieldArray, useForm } from 'react-hook-form'
import { z } from 'zod'

import { ConfirmDialog, FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Input,
  Label,
  Skeleton,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ItemPicker, WarehouseSelect } from '@/features/inventory/pickers'
import { VendorPicker } from '@/features/vendors/vendor-picker'
import { useVendorOptions } from '@/features/vendors/vendor-types'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatDate, toDateInput, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { cn, indexById } from '@/lib/utils'

import { type RequisitionRow } from './procurement-types'
import {
  MAX_RFQ_VENDORS,
  isRfqEditable,
  rfqKeys,
  type RfqCommand,
  type RfqCreatePayload,
  type RfqDetail,
  type RfqLinePayload,
  type RfqRow,
  type RfqUpdatePayload,
} from './rfq-model'

// A NUMERIC(18,4) on the API side, and a string on the wire in both directions.
const QUANTITY = /^\d{1,14}(\.\d{1,4})?$/
const CURRENCY = /^[A-Z]{3}$/

/** `ReasonRequest` / `ApproveRequest` both cap the reason at 255 characters. */
const MAX_REASON = 255

// =============================================================================
// Lifecycle commands
// =============================================================================

const COMMAND_COPY: Record<
  RfqCommand,
  {
    title: string
    description: string
    confirmLabel: string
    destructive: boolean
    /** True where the API's own schema makes the reason mandatory. */
    reasonRequired: boolean
    success: string
  }
> = {
  send: {
    title: 'Send this RFQ to its vendors?',
    description:
      'The enquiry is issued to the vendors named on it and stops being editable - they will be quoting against these lines.',
    confirmLabel: 'Send RFQ',
    destructive: false,
    // `ApproveRequest`: the reason is optional, and the API records "Sent to
    // vendors" against the audit trail when none is given.
    reasonRequired: false,
    success: 'RFQ sent',
  },
  close: {
    title: 'Close this RFQ?',
    description:
      'Use this once a vendor has been chosen. Nothing further is expected from the others, and a closed RFQ cannot be reopened.',
    confirmLabel: 'Close RFQ',
    destructive: false,
    reasonRequired: false,
    success: 'RFQ closed',
  },
  cancel: {
    title: 'Cancel this RFQ?',
    description:
      'The enquiry is abandoned and no quotation can be accepted against it. A cancelled RFQ cannot be reopened, and the reason you record is what the vendors are told.',
    confirmLabel: 'Cancel RFQ',
    destructive: true,
    // `ReasonRequest`: the reason is required and must be at least 3 characters.
    reasonRequired: true,
    success: 'RFQ cancelled',
  },
}

/**
 * Confirm and carry out one lifecycle command.
 *
 * All three endpoints answer with an `RfqSummary` - the header only, without
 * lines - so the reply is typed as the row it actually is and the detail screen
 * refreshes through the invalidation rather than off this response.
 *
 * Every command is gated on `rfq.manage` at the API. The caller decides whether
 * to offer the button; this dialog does not re-check, because a dialog that is
 * open has already been asked for.
 */
export function RfqCommandDialog({
  rfq,
  command,
  onOpenChange,
}: {
  rfq: { id: string; code: string; vendor_ids: string[] } | null
  command: RfqCommand | null
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')
  const [touched, setTouched] = useState(false)

  const copy = command ? COMMAND_COPY[command] : null

  const close = () => {
    setReason('')
    setTouched(false)
    onOpenChange(false)
  }

  const run = useMutation({
    mutationFn: ({ text }: { text: string }) => {
      if (!rfq || !command) throw new Error('No RFQ selected')
      return api.post<RfqRow>(`/procurement/rfqs/${rfq.id}/${command}`, {
        // Cancel demands a reason; send and close accept null, which is how
        // "no note was recorded" is expressed rather than an empty string.
        reason: text === '' ? null : text,
      })
    },
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: rfqKeys.all })
      toast.success(
        copy?.success ?? 'RFQ updated',
        `${updated.code} is now ${updated.status.toLowerCase()}.`,
      )
      close()
    },
  })

  const trimmed = reason.trim()
  const tooShort = trimmed.length < 3
  const tooLong = trimmed.length > MAX_REASON

  return (
    <ConfirmDialog
      open={Boolean(rfq && command)}
      onOpenChange={(next) => {
        if (!next) close()
      }}
      title={copy?.title ?? ''}
      description={
        rfq && copy
          ? command === 'send'
            ? `${copy.description} (${rfq.code}, ${rfq.vendor_ids.length} vendor${rfq.vendor_ids.length === 1 ? '' : 's'})`
            : `${copy.description} (${rfq.code})`
          : undefined
      }
      confirmLabel={copy?.confirmLabel ?? 'Confirm'}
      destructive={copy?.destructive}
      loading={run.isPending}
      onConfirm={() => {
        if (!copy) return
        if ((copy.reasonRequired && tooShort) || tooLong) {
          setTouched(true)
          return
        }
        run.mutate({ text: trimmed })
      }}
    >
      <div>
        <Label htmlFor="rfq-command-reason" required={copy?.reasonRequired}>
          {copy?.reasonRequired ? 'Reason' : 'Note'}
        </Label>
        <Textarea
          id="rfq-command-reason"
          value={reason}
          maxLength={MAX_REASON}
          onChange={(event) => setReason(event.target.value)}
          className="mt-1.5"
          aria-invalid={touched && ((copy?.reasonRequired === true && tooShort) || tooLong)}
        />
        {touched && copy?.reasonRequired && tooShort && (
          <FieldError message="Record at least three characters of reason" />
        )}
        {touched && tooLong && <FieldError message={`At most ${MAX_REASON} characters`} />}
        {!copy?.reasonRequired && (
          <p className="mt-1 text-xs text-muted-foreground">
            Optional. Whatever you record here goes onto the audit trail with the transition.
          </p>
        )}
      </div>
    </ConfirmDialog>
  )
}

// =============================================================================
// The form
// =============================================================================

/**
 * One schema for both ways the dialog is used.
 *
 * The line rule genuinely differs between them - a create may carry no lines of
 * its own, because the API copies the requisition's open lines instead, while an
 * edit may not, because `PATCH` replaces the line set with whatever it is given
 * and an empty one would leave an enquiry with nothing to quote. That second
 * rule is checked in the submit handler rather than encoded here on purpose:
 * `useForm` takes its resolver once, and this dialog changes between create and
 * edit while it stays mounted, so a schema built from the mode would be the
 * wrong one exactly when it mattered.
 */
const schema = z
  .object({
    warehouse_id: z.string().min(1, 'Choose the warehouse this material is for'),
    requisition_id: z.string(),
    rfq_date: z.string().min(1, 'The RFQ date is required'),
    response_due_date: z.string(),
    required_date: z.string(),
    currency: z
      .string()
      .trim()
      .toUpperCase()
      .regex(CURRENCY, 'Three capital letters, such as INR'),
    vendor_ids: z
      .array(z.string())
      .max(MAX_RFQ_VENDORS, `At most ${MAX_RFQ_VENDORS} vendors on one RFQ`),
    terms: z.string().trim().max(4000, 'At most 4000 characters'),
    notes: z.string().trim().max(4000, 'At most 4000 characters'),
    lines: z.array(
      z.object({
        item_id: z.string().min(1, 'Choose an item'),
        quantity: z
          .string()
          .trim()
          .regex(QUANTITY, 'Enter a quantity with up to 4 decimal places')
          .refine((v) => toDecimal(v).greaterThan(0), 'Quantity must be greater than zero'),
        required_date: z.string(),
        specification: z.string().trim().max(4000, 'At most 4000 characters'),
        // Carried through an edit rather than edited. See the module comment.
        description: z.string(),
        uom_id: z.string(),
        requisition_line_id: z.string(),
        notes: z.string(),
      }),
    ),
  })
  .superRefine((values, ctx) => {
    // A create with neither lines nor a requisition asks for nothing at all.
    if (values.lines.length === 0 && values.requisition_id === '') {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['lines'],
        message: 'Add a line, or choose the requisition whose open lines should be quoted',
      })
    }
    // Asking for replies before the enquiry exists is a data-entry slip that
    // would show up immediately as an overdue response.
    if (values.response_due_date && values.rfq_date && values.response_due_date < values.rfq_date) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['response_due_date'],
        message: 'Vendors cannot be asked to reply before the RFQ is raised',
      })
    }
    // And replies that would arrive after the material was needed cannot be
    // acted on, which is the other end of the same mistake.
    if (
      values.response_due_date &&
      values.required_date &&
      values.response_due_date > values.required_date
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['response_due_date'],
        message: 'Replies would arrive after the material is needed',
      })
    }
  })

type RfqFormValues = z.infer<typeof schema>

function emptyLine(): RfqFormValues['lines'][number] {
  return {
    item_id: '',
    quantity: '',
    required_date: '',
    specification: '',
    description: '',
    uom_id: '',
    requisition_line_id: '',
    notes: '',
  }
}

/** A row the buyer has not started filling in, and can safely be discarded. */
const isBlankLine = (line: RfqFormValues['lines'][number]): boolean =>
  line.item_id === '' && line.quantity.trim() === ''

function emptyValues(): RfqFormValues {
  return {
    warehouse_id: '',
    requisition_id: '',
    rfq_date: toDateInput(new Date()),
    response_due_date: '',
    required_date: '',
    currency: 'INR',
    vendor_ids: [],
    terms: '',
    notes: '',
    lines: [emptyLine()],
  }
}

function valuesFrom(rfq: RfqDetail): RfqFormValues {
  return {
    warehouse_id: rfq.warehouse_id,
    requisition_id: rfq.requisition_id ?? '',
    rfq_date: rfq.rfq_date,
    response_due_date: rfq.response_due_date ?? '',
    required_date: rfq.required_date ?? '',
    currency: rfq.currency,
    vendor_ids: rfq.vendor_ids,
    terms: rfq.terms ?? '',
    notes: rfq.notes ?? '',
    lines: (rfq.lines ?? []).map((line) => ({
      item_id: line.item_id,
      // The API sends "10.0000" for a whole number. Round-tripping it through
      // decimal.js gives the buyer back what they typed rather than four
      // decimal places of noise - and never through a float.
      quantity: toDecimal(line.quantity).toFixed(),
      required_date: line.required_date ?? '',
      specification: line.specification ?? '',
      description: line.description ?? '',
      uom_id: line.uom_id,
      requisition_line_id: line.requisition_line_id ?? '',
      notes: line.notes ?? '',
    })),
  }
}

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

function buildLines(lines: RfqFormValues['lines']): RfqLinePayload[] {
  return lines.map((line, index) => ({
    // Renumbered from the form's order, so removing the middle line of three
    // does not leave a gap the API would store verbatim.
    line_number: index + 1,
    item_id: line.item_id,
    // Stays a string on the wire; the API parses a Decimal from it.
    quantity: line.quantity.trim(),
    required_date: orNull(line.required_date),
    specification: orNull(line.specification),
    description: orNull(line.description),
    uom_id: orNull(line.uom_id),
    requisition_line_id: orNull(line.requisition_line_id),
    notes: orNull(line.notes),
  }))
}

// ------------------------------------------------------ requisition field ---

/**
 * Pick the approved requisition an RFQ is being raised against.
 *
 * `open_only` is the API's own "unmet approved demand" narrowing, which is
 * `(APPROVED, CONVERTED)` - exactly the two statuses `create_rfq` will quote.
 * Filtering on `status=APPROVED` instead would hide the requisitions that have
 * been partly ordered and still have lines left to source.
 *
 * Searched on the server, like every other picker in this application.
 */
function RequisitionPicker({
  value,
  onChange,
}: {
  value: string | null
  onChange: (requisitionId: string | null) => void
}) {
  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [debouncedTerm, setDebouncedTerm] = useState('')
  const [picked, setPicked] = useState<RequisitionRow | null>(null)

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedTerm(term.trim()), 300)
    return () => clearTimeout(timer)
  }, [term])

  const params = useMemo(
    () => ({
      limit: 20,
      with_total: false,
      open_only: true,
      sort_by: 'requisition_date',
      sort_dir: 'desc' as const,
      ...(debouncedTerm ? { search: debouncedTerm } : {}),
    }),
    [debouncedTerm],
  )

  const results = useQuery({
    queryKey: [...rfqKeys.all, 'requisition-options', params] as const,
    queryFn: () =>
      api.getPage<RequisitionRow>('/procurement/purchase-requisitions', { params }),
    enabled: open,
    staleTime: staleTimes.transactional,
  })

  const rows = results.data?.data ?? []
  const selected = value && picked?.id === value ? picked : null

  return (
    <div className="flex items-center gap-1">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            id="rfq-requisition"
            type="button"
            variant="outline"
            role="combobox"
            aria-expanded={open}
            aria-label="Requisition"
            className={cn(
              'h-9 min-w-[200px] flex-1 justify-between font-normal',
              !value && 'text-muted-foreground',
            )}
          >
            <span className="truncate">
              {selected ? selected.code : value ? 'Requisition selected' : 'Not from a requisition'}
            </span>
            <ChevronsUpDown className="opacity-50" aria-hidden />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-[24rem] p-0">
          <div className="relative border-b p-2">
            <Search
              className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden
            />
            <Input
              autoFocus
              value={term}
              onChange={(event) => setTerm(event.target.value)}
              placeholder="Search by number or purpose"
              aria-label="Search approved requisitions"
              className="pl-8"
            />
          </div>

          <div className="max-h-64 overflow-y-auto p-1" role="listbox" aria-label="Requisitions">
            {results.isLoading && (
              <p className="flex items-center gap-2 px-2 py-6 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Searching...
              </p>
            )}

            {!results.isLoading && results.error && (
              <p className="px-2 py-6 text-sm text-destructive">
                {(results.error as Error).message}
              </p>
            )}

            {!results.isLoading && !results.error && rows.length === 0 && (
              <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                No approved requisition still has anything left to source.
              </p>
            )}

            {rows.map((requisition) => (
              <button
                key={requisition.id}
                type="button"
                role="option"
                aria-selected={requisition.id === value}
                onClick={() => {
                  setPicked(requisition)
                  onChange(requisition.id)
                  setOpen(false)
                }}
                className="flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
              >
                <Check
                  className={cn(
                    'mt-0.5 h-4 w-4 shrink-0',
                    requisition.id !== value && 'invisible',
                  )}
                  aria-hidden
                />
                <span className="min-w-0">
                  <span className="block truncate font-medium">{requisition.code}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    Needed {formatDate(requisition.required_date)}
                    {requisition.purpose ? ` - ${requisition.purpose}` : ''}
                  </span>
                </span>
              </button>
            ))}
          </div>
        </PopoverContent>
      </Popover>

      {value && (
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Clear requisition"
          onClick={() => {
            setPicked(null)
            onChange(null)
          }}
        >
          <X />
        </Button>
      )}
    </div>
  )
}

// ---------------------------------------------------------- vendor field ----

/**
 * The invitation list.
 *
 * Names are resolved through `/vendors/options`, the bounded projection the API
 * publishes for exactly this - one cached reference query rather than a lookup
 * per chip. A vendor that has since been suspended is not in that list at all,
 * so it is reported as such rather than rendered as a UUID.
 */
function VendorInvitations({
  value,
  onChange,
}: {
  value: string[]
  onChange: (vendorIds: string[]) => void
}) {
  const { can } = useAuth()
  const vendors = useVendorOptions(can(P.VENDOR_VIEW))
  const vendorById = useMemo(() => indexById(vendors.data), [vendors.data])
  const atLimit = value.length >= MAX_RFQ_VENDORS

  return (
    <div className="space-y-2">
      <VendorPicker
        id="rfq-vendor-add"
        label="Invite vendors"
        // Always null: the picker is an "add" control here, so it returns to
        // its placeholder after each choice instead of showing the last one.
        value={null}
        onChange={(vendorId) => {
          // Silently ignoring a repeat is right - `create_rfq` de-duplicates
          // the list anyway, and a warning about a vendor already invited would
          // be noise.
          if (vendorId && !value.includes(vendorId)) onChange([...value, vendorId])
        }}
        placeholder={atLimit ? `${MAX_RFQ_VENDORS} vendors is the maximum` : 'Search vendors'}
        disabled={atLimit}
      />

      {value.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          An RFQ can be saved with no vendors, but it cannot be sent until at least one is named.
        </p>
      ) : (
        <ul className="flex flex-wrap gap-1.5">
          {value.map((vendorId) => {
            const vendor = vendorById.get(vendorId)
            return (
              <li key={vendorId}>
                <Badge variant="secondary" className="pr-1">
                  {vendor ? `${vendor.code} - ${vendor.name}` : 'Vendor no longer listed'}
                  {vendor && !vendor.is_approved && (
                    <span className="text-warning" title="Not an approved vendor">
                      (unapproved)
                    </span>
                  )}
                  <button
                    type="button"
                    aria-label={`Remove ${vendor ? vendor.name : 'this vendor'} from the RFQ`}
                    onClick={() => onChange(value.filter((id) => id !== vendorId))}
                    className="ml-0.5 rounded-full p-0.5 hover:bg-background/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <X className="h-3 w-3" aria-hidden />
                  </button>
                </Badge>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

// ------------------------------------------------------------ form dialog ---

/**
 * Raise a new RFQ, or edit a draft one.
 *
 * `rfqId` null means create. Editing loads the RFQ through the same query key
 * the detail screen uses, so opening the dialog from there costs no request.
 */
export function RfqFormDialog({
  open,
  onOpenChange,
  rfqId,
  onCreated,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  rfqId: string | null
  /** Called with the new RFQ so a list can navigate to it. */
  onCreated?: (rfq: RfqDetail) => void
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const isEdit = Boolean(rfqId)

  const detail = useQuery({
    queryKey: rfqKeys.detail(rfqId ?? ''),
    queryFn: () => api.get<RfqDetail>(`/procurement/rfqs/${rfqId}`),
    enabled: open && Boolean(rfqId),
    staleTime: staleTimes.transactional,
  })

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    setValue,
    getValues,
    watch,
    formState: { errors },
  } = useForm<RfqFormValues>({
    resolver: zodResolver(schema),
    defaultValues: emptyValues(),
  })

  const { fields, append, remove, replace } = useFieldArray({ control, name: 'lines' })

  const loaded = detail.data
  useEffect(() => {
    if (!open) return
    reset(loaded && rfqId ? valuesFrom(loaded) : emptyValues())
    // `loaded` is compared by presence: re-running on every refetch would throw
    // away edits in progress.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, rfqId, Boolean(loaded), reset])

  const save = useMutation({
    mutationFn: (values: RfqFormValues) => {
      if (rfqId) {
        // Typed as the contract's own body: `RfqUpdate` forbids unknown keys,
        // and a field this form invented would be a 422 at run time rather
        // than a compile error.
        //
        // It also *ignores every null it is given*, so this can replace a date
        // but cannot clear one. The lines are the exception - they are replaced
        // wholesale - which is why they are always sent.
        const payload: RfqUpdatePayload = {
          warehouse_id: values.warehouse_id,
          response_due_date: orNull(values.response_due_date),
          required_date: orNull(values.required_date),
          currency: values.currency,
          vendor_ids: values.vendor_ids,
          terms: orNull(values.terms),
          notes: orNull(values.notes),
          lines: buildLines(values.lines),
        }
        return api.patch<RfqDetail>(`/procurement/rfqs/${rfqId}`, payload)
      }
      const payload: RfqCreatePayload = {
        warehouse_id: values.warehouse_id,
        rfq_date: values.rfq_date,
        response_due_date: orNull(values.response_due_date),
        required_date: orNull(values.required_date),
        currency: values.currency,
        vendor_ids: values.vendor_ids,
        terms: orNull(values.terms),
        notes: orNull(values.notes),
        ...(values.requisition_id ? { requisition_id: values.requisition_id } : {}),
        // Omitted entirely rather than sent empty when the requisition's own
        // open lines are what is being quoted: `create_rfq` copies them only
        // when `lines` is absent, and an empty array is refused outright as
        // "An RFQ needs at least one line".
        ...(values.lines.length > 0 ? { lines: buildLines(values.lines) } : {}),
      }
      return api.post<RfqDetail>('/procurement/rfqs', payload)
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: rfqKeys.all })
      toast.success(
        isEdit ? 'RFQ updated' : 'RFQ raised',
        isEdit
          ? `${saved.code} still has to be sent before any vendor sees it.`
          : `${saved.code} is a draft with ${saved.lines?.length ?? 0} line(s). Send it when the vendor list is right.`,
      )
      onOpenChange(false)
      if (!isEdit) onCreated?.(saved)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const requisitionId = watch('requisition_id')
  const vendorIds = watch('vendor_ids')
  const lineCount = fields.length

  // The API needs a warehouse on every RFQ, so a principal who cannot read the
  // warehouse list has nothing to choose and would fail on a field that was
  // never shown.
  const canChooseWarehouse = can(P.WAREHOUSE_VIEW)
  const sentAndUneditable = Boolean(loaded && !isRfqEditable(loaded.status))

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit ${loaded?.code ?? 'RFQ'}` : 'New request for quotation'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'A draft RFQ can still be changed. Once it is sent, the vendors are quoting against these lines and it is fixed.'
              : 'Raised as a draft. Nothing reaches a vendor until you send it, and unapproved vendors may be asked for a price.'}
          </DialogDescription>
        </DialogHeader>

        {isEdit && detail.isLoading && (
          <DialogBody className="space-y-3" aria-hidden>
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-2/3" />
            <Skeleton className="h-24 w-full" />
          </DialogBody>
        )}

        {isEdit && !detail.isLoading && detail.error && (
          <DialogBody>
            <Alert variant="destructive">
              <AlertTitle>Could not load this RFQ</AlertTitle>
              <AlertDescription>{(detail.error as Error).message}</AlertDescription>
            </Alert>
          </DialogBody>
        )}

        {(!isEdit || (!detail.isLoading && !detail.error)) && (
          <form
            onSubmit={handleSubmit((values) => {
              // An edit sends the whole line set, so saving an empty one would
              // leave an enquiry with nothing to quote. The schema cannot state
              // this - see the comment on it - so the mode-dependent half of
              // the rule is checked here, where the mode is current.
              if (isEdit && values.lines.length === 0) {
                setError('lines', {
                  type: 'manual',
                  message: 'An RFQ needs at least one line',
                })
                return
              }
              save.mutate(values)
            })}
            className="flex min-h-0 flex-col gap-4"
            noValidate
          >
            <DialogBody className="space-y-5">
              {sentAndUneditable && (
                <Alert variant="warning">
                  <AlertTitle>This RFQ has already been sent</AlertTitle>
                  <AlertDescription>
                    Vendors are quoting against the lines as they were issued, so the API will
                    refuse any change. Cancel it and raise a new one if the enquiry was wrong.
                  </AlertDescription>
                </Alert>
              )}

              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {canChooseWarehouse ? (
                  <div>
                    <Controller
                      control={control}
                      name="warehouse_id"
                      render={({ field }) => (
                        <WarehouseSelect
                          id="rfq-warehouse"
                          label="Deliver to"
                          value={field.value}
                          onChange={field.onChange}
                          invalid={Boolean(errors.warehouse_id)}
                        />
                      )}
                    />
                    <FieldError message={errors.warehouse_id?.message} />
                  </div>
                ) : (
                  <div className="sm:col-span-2 lg:col-span-3">
                    <Alert variant="warning">
                      <AlertTitle>You cannot raise an RFQ from here</AlertTitle>
                      <AlertDescription>
                        Every RFQ has to state the warehouse the material is for, and your account
                        cannot read the warehouse list. Ask somebody who can to raise it.
                      </AlertDescription>
                    </Alert>
                  </div>
                )}

                <div>
                  <Label htmlFor="rfq-date" required>
                    RFQ date
                  </Label>
                  <Input
                    id="rfq-date"
                    type="date"
                    {...register('rfq_date')}
                    // The API has no way to move this once the document exists:
                    // `RfqUpdate` does not carry it.
                    disabled={isEdit}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.rfq_date)}
                  />
                  <FieldError message={errors.rfq_date?.message} />
                  {isEdit && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      Fixed once the RFQ is raised.
                    </p>
                  )}
                </div>

                <div>
                  <Label htmlFor="rfq-response-due">Replies due by</Label>
                  <Input
                    id="rfq-response-due"
                    type="date"
                    {...register('response_due_date')}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.response_due_date)}
                  />
                  <FieldError message={errors.response_due_date?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    What the chasing list on the RFQ board is measured against.
                  </p>
                </div>

                <div>
                  <Label htmlFor="rfq-required">Material required by</Label>
                  <Input
                    id="rfq-required"
                    type="date"
                    {...register('required_date')}
                    className="mt-1.5"
                    aria-invalid={Boolean(errors.required_date)}
                  />
                  <FieldError message={errors.required_date?.message} />
                </div>

                <div>
                  <Label htmlFor="rfq-currency" required>
                    Currency
                  </Label>
                  <Input
                    id="rfq-currency"
                    {...register('currency')}
                    autoComplete="off"
                    maxLength={3}
                    className="mt-1.5 uppercase"
                    aria-invalid={Boolean(errors.currency)}
                  />
                  <FieldError message={errors.currency?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    Quotations in another currency cannot be compared against these.
                  </p>
                </div>

                {/* Only offered on a create: `RfqUpdate` carries no
                    requisition, so the link is fixed once the RFQ exists. And
                    only to a principal who can read requisitions - firing the
                    search otherwise earns a 403 they cannot act on. */}
                {!isEdit && can(P.PURCHASE_REQUISITION_VIEW) && (
                  <div className="sm:col-span-2 lg:col-span-1">
                    <Label htmlFor="rfq-requisition">Raised against</Label>
                    <div className="mt-1.5">
                      <Controller
                        control={control}
                        name="requisition_id"
                        render={({ field }) => (
                          <RequisitionPicker
                            value={field.value || null}
                            onChange={(id) => {
                              field.onChange(id ?? '')
                              // A requisition brings its own lines, and the API
                              // copies them only when none are sent. The blank
                              // starter row would therefore both fail
                              // validation and silently switch the RFQ off the
                              // copy path, so it is dropped - but only while
                              // every line is still untouched.
                              if (id && getValues('lines').every(isBlankLine)) replace([])
                              // And clearing the requisition must leave
                              // somewhere to type, or the form asks for nothing.
                              if (!id && getValues('lines').length === 0) replace([emptyLine()])
                            }}
                          />
                        )}
                      />
                    </div>
                    <FieldError message={errors.requisition_id?.message} />
                  </div>
                )}
              </div>

              <div>
                <h3 className="text-sm font-medium">Vendors</h3>
                <p className="mb-2 mt-0.5 text-xs text-muted-foreground">
                  Approval is not required to be asked for a price - that is often how the evidence
                  to approve a vendor is gathered. {vendorIds.length} of {MAX_RFQ_VENDORS} invited.
                </p>
                <Controller
                  control={control}
                  name="vendor_ids"
                  render={({ field }) => (
                    <VendorInvitations value={field.value} onChange={field.onChange} />
                  )}
                />
                <FieldError message={errors.vendor_ids?.root?.message ?? errors.vendor_ids?.message} />
              </div>

              <div>
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-sm font-medium">Lines</h3>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => append(emptyLine())}
                  >
                    <Plus />
                    Add line
                  </Button>
                </div>
                <FieldError message={errors.lines?.root?.message ?? errors.lines?.message} />

                {lineCount === 0 && requisitionId && (
                  <Alert variant="info" className="mt-3">
                    <AlertTitle>The requisition&rsquo;s own lines will be quoted</AlertTitle>
                    <AlertDescription>
                      Every line on it that has not already been ordered is copied onto this RFQ.
                      Add a line here only to ask for something the requisition does not cover - the
                      moment you do, the copy stops and these lines are what is sent.
                    </AlertDescription>
                  </Alert>
                )}

                <div className="mt-3 space-y-3">
                  {fields.map((field, index) => (
                    <div
                      key={field.id}
                      className="grid gap-3 rounded-lg border p-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,2fr)_auto]"
                    >
                      <div>
                        <Controller
                          control={control}
                          name={`lines.${index}.item_id`}
                          render={({ field: itemField }) => (
                            <ItemPicker
                              id={`rfq-line-item-${index}`}
                              label={`Line ${index + 1} item`}
                              value={itemField.value || null}
                              onChange={(itemId) => {
                                itemField.onChange(itemId ?? '')
                                // The description, unit and requisition link
                                // carried on this line describe the item that
                                // was there before, and an edit sends them back
                                // verbatim. Swapping the item without clearing
                                // them would keep the old item's name and unit
                                // and leave the line pointing at a requisition
                                // line for something else entirely.
                                setValue(`lines.${index}.description`, '')
                                setValue(`lines.${index}.uom_id`, '')
                                setValue(`lines.${index}.requisition_line_id`, '')
                              }}
                              placeholder="Search the item master"
                              // The API refuses an item that is not flagged
                              // purchasable, so it is never offered here.
                              filters={{ can_purchase: true }}
                              emptyMessage="No purchasable item matches that search."
                              invalid={Boolean(errors.lines?.[index]?.item_id)}
                            />
                          )}
                        />
                        <FieldError message={errors.lines?.[index]?.item_id?.message} />
                      </div>

                      <div>
                        <Label htmlFor={`rfq-line-qty-${index}`} required>
                          Quantity
                        </Label>
                        <Input
                          id={`rfq-line-qty-${index}`}
                          {...register(`lines.${index}.quantity`)}
                          inputMode="decimal"
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.lines?.[index]?.quantity)}
                        />
                        <FieldError message={errors.lines?.[index]?.quantity?.message} />
                        <p className="mt-1 text-xs text-muted-foreground">
                          In the item&rsquo;s purchase unit.
                        </p>
                      </div>

                      <div>
                        <Label htmlFor={`rfq-line-required-${index}`}>Required by</Label>
                        <Input
                          id={`rfq-line-required-${index}`}
                          type="date"
                          {...register(`lines.${index}.required_date`)}
                          className="mt-1.5"
                        />
                      </div>

                      <div>
                        <Label htmlFor={`rfq-line-spec-${index}`}>Specification</Label>
                        <Input
                          id={`rfq-line-spec-${index}`}
                          {...register(`lines.${index}.specification`)}
                          autoComplete="off"
                          placeholder="Finish, grade, drawing revision"
                          className="mt-1.5"
                          aria-invalid={Boolean(errors.lines?.[index]?.specification)}
                        />
                        <FieldError message={errors.lines?.[index]?.specification?.message} />
                      </div>

                      <div className="flex items-end">
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          aria-label={`Remove line ${index + 1}`}
                          onClick={() => remove(index)}
                        >
                          <Trash2 className="text-destructive" />
                        </Button>
                      </div>

                      {/* Never shown, always preserved: an edit rebuilds the
                          whole line set from this payload. */}
                      <input type="hidden" {...register(`lines.${index}.description`)} />
                      <input type="hidden" {...register(`lines.${index}.uom_id`)} />
                      <input type="hidden" {...register(`lines.${index}.requisition_line_id`)} />
                      <input type="hidden" {...register(`lines.${index}.notes`)} />
                    </div>
                  ))}
                </div>
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <Label htmlFor="rfq-terms">Terms</Label>
                  <Textarea id="rfq-terms" {...register('terms')} className="mt-1.5" />
                  <FieldError message={errors.terms?.message} />
                  <p className="mt-1 text-xs text-muted-foreground">
                    Payment, freight and warranty conditions the vendors are quoting against.
                  </p>
                </div>

                <div>
                  <Label htmlFor="rfq-notes">Notes</Label>
                  <Textarea id="rfq-notes" {...register('notes')} className="mt-1.5" />
                  <FieldError message={errors.notes?.message} />
                </div>
              </div>
            </DialogBody>

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={save.isPending}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                loading={save.isPending}
                disabled={!canChooseWarehouse || sentAndUneditable}
              >
                {isEdit ? 'Save changes' : 'Raise draft RFQ'}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}

/**
 * Building the cartons for one packing order.
 *
 * The screen is organised around the physical job: start the order, open a
 * carton, put things in it, seal it, repeat, then complete and hand it to a
 * consignment. Four details are what make it usable on a bench rather than at a
 * desk:
 *
 * * The running summary - cartons and gross weight - is always visible, because
 *   that is what goes on the transport document and what the freight is priced
 *   on.
 * * Every line shows what is still unpacked, so the packer never has to work out
 *   the remainder.
 * * Sealing is confirmed and irreversible. A sealed carton is a taped box on a
 *   pallet; letting the system edit its contents would make the paperwork lie.
 *   Sealing also *issues the box itself* from stock, so it is not a free action.
 * * A serial-tracked item is packed by naming its units, not by typing numbers
 *   into a box. docs/assumptions.md section 23 gates every movement that changes
 *   whose hands hold a unit, and a sealed carton is one, so the API takes serial
 *   *ids* from the stock register - one per unit - and refuses anything else.
 *
 * Two API shapes drive the structure. Cartons are their own paged endpoint, not
 * part of the order payload; and a carton's contents are *replaced* as a set
 * with a recorded reason, so adding one item means sending the whole box back.
 *
 * The packing list is a PDF build, so it is queued rather than awaited. The
 * dialog polls the report job and turns into a download when it finishes -
 * nothing blocks while it runs.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Boxes,
  FileText,
  Lock,
  Package,
  PackageCheck,
  PackagePlus,
  Play,
  Printer,
  Plus,
  Trash2,
  Truck,
  Weight,
} from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'

import {
  ConfirmDialog,
  DescriptionList,
  EmptyState,
  FieldError,
  StatusBadge,
} from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Checkbox,
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { BatchSelect, SerialPicker } from '@/features/dispatch/stock-pickers'
import { useItemFacts, type DispatchRow } from '@/features/dispatch/dispatch-types'
import { ReportJobPanel } from '@/features/reports/job-panel'
import { useReportJob, type AcceptedJobData } from '@/features/reports/report-jobs'
import { api } from '@/lib/api-client'
import {
  LOOKUP_LIST_LIMIT,
  REASON_MAX_LENGTH,
  REASON_MIN_LENGTH,
  SEARCH_DEBOUNCE_MS,
} from '@/lib/constants'
import {
  formatDate,
  formatDateTime,
  formatNumber,
  formatQuantity,
  humanise,
  toDecimal,
} from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import {
  CARTON_TYPES,
  MEASURE_INPUT,
  QUANTITY_INPUT,
  isCloseable,
  isCompletable,
  isHandOverable,
  isPackable,
  isPackingCancellable,
  isStartable,
  packingKeys,
  type Carton,
  type CartonContent,
  type CartonContentIn,
  type CartonLabel,
  type PackingCompletionResult,
  type PackingMaterialRow,
  type PackingOrderDetail,
  type PackingOrderLine,
  type PackingOrderRow,
} from './packing-types'

/** One page of cartons. The API caps a page at 200. */
const CARTON_PAGE = 200

const orNull = (value: string): string | null => (value.trim() === '' ? null : value.trim())

/** What is left to pack on a line, as decimals - never as doubles. */
function pendingOf(line: PackingOrderLine): string {
  const pending = toDecimal(line.quantity_to_pack).minus(toDecimal(line.packed_quantity))
  return pending.greaterThan(0) ? pending.toFixed() : '0'
}

/**
 * The whole content set of a carton, as `CartonContentsReplace` wants it back.
 *
 * The read shape names the order line by id and the write shape names it by
 * line number, so keeping a content row unchanged still means translating it.
 */
function toContentInput(contents: CartonContent[], lines: PackingOrderLine[]): CartonContentIn[] {
  const numberOf = new Map(lines.map((line) => [line.id, line.line_number]))
  return contents
    .filter((content) => numberOf.has(content.packing_order_line_id))
    .map((content) => ({
      line_number: numberOf.get(content.packing_order_line_id)!,
      quantity: content.quantity,
      batch_id: content.batch_id,
      serial_ids: content.serial_ids,
      net_weight_kg: content.net_weight_kg,
    }))
}

// ------------------------------------------------------- a recorded reason ---

/** Every packing transition is audited, so none of them is a bare confirm. */
function ReasonDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  destructive = false,
  loading,
  onConfirm,
  children,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: string
  confirmLabel: string
  destructive?: boolean
  loading: boolean
  onConfirm: (reason: string) => void
  children?: ReactNode
}) {
  const [reason, setReason] = useState('')
  const [touched, setTouched] = useState(false)

  // `Reason` is bounded 3..255. A 422 keyed on it is one the mutation cache
  // stays quiet about, on the assumption the form shows it - so both the
  // missing case and the out-of-band case are caught before the request.
  const trimmed = reason.trim()
  const missing = trimmed === ''
  const outOfBounds =
    !missing && (trimmed.length < REASON_MIN_LENGTH || trimmed.length > REASON_MAX_LENGTH)
  const invalid = missing || outOfBounds

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next)
        if (!next) {
          setReason('')
          setTouched(false)
        }
      }}
      title={title}
      description={description}
      confirmLabel={confirmLabel}
      destructive={destructive}
      loading={loading}
      onConfirm={() => {
        if (invalid) {
          setTouched(true)
          return
        }
        onConfirm(trimmed)
      }}
    >
      <div className="space-y-3">
        {children}
        <div>
          <Label htmlFor="packing-reason" required>
            Reason
          </Label>
          <Input
            id="packing-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            autoComplete="off"
            maxLength={REASON_MAX_LENGTH}
            className="mt-1.5"
            aria-invalid={touched && invalid}
          />
          {touched && missing && (
            <FieldError message="The API records this against the order - it needs a reason" />
          )}
          {touched && outOfBounds && (
            <FieldError
              message={`The audit reason must be between ${REASON_MIN_LENGTH} and ${REASON_MAX_LENGTH} characters`}
            />
          )}
        </div>
      </div>
    </ConfirmDialog>
  )
}

// ------------------------------------------------------------ add carton ----

function AddCartonForm({ order }: { order: PackingOrderDetail }) {
  const queryClient = useQueryClient()
  const [materialId, setMaterialId] = useState('')
  const [materialQuantity, setMaterialQuantity] = useState('1')
  const [cartonType, setCartonType] = useState<string>('BOX')
  const [length, setLength] = useState('')
  const [width, setWidth] = useState('')
  const [height, setHeight] = useState('')
  const [tare, setTare] = useState('')
  const [problem, setProblem] = useState<string | null>(null)

  // Which boxes this warehouse actually holds. The endpoint insists on the
  // warehouse, because a carton is issued from the stock standing there. It
  // also searches, so a store with more packing materials than one page holds
  // is narrowed on the server rather than silently cut at the page boundary.
  const [materialSearch, setMaterialSearch] = useState('')
  const [debouncedMaterialSearch, setDebouncedMaterialSearch] = useState('')
  useEffect(() => {
    const timer = setTimeout(
      () => setDebouncedMaterialSearch(materialSearch.trim()),
      SEARCH_DEBOUNCE_MS,
    )
    return () => clearTimeout(timer)
  }, [materialSearch])

  const materials = useQuery({
    queryKey: packingKeys.materials(order.warehouse_id, debouncedMaterialSearch),
    queryFn: () =>
      api.getPage<PackingMaterialRow>('/packing/packing-materials', {
        params: {
          warehouse_id: order.warehouse_id,
          limit: LOOKUP_LIST_LIMIT,
          with_total: false,
          ...(debouncedMaterialSearch ? { search: debouncedMaterialSearch } : {}),
        },
      }),
    staleTime: staleTimes.transactional,
  })
  const moreMaterials = materials.data?.pagination?.has_next ?? false

  const addCarton = useMutation({
    mutationFn: () =>
      api.post<Carton>(`/packing/${order.id}/cartons`, {
        packing_material_item_id: materialId,
        packing_material_quantity: materialQuantity.trim() || '1',
        carton_type: cartonType,
        length_cm: orNull(length),
        width_cm: orNull(width),
        height_cm: orNull(height),
        tare_weight_kg: tare.trim() === '' ? '0' : tare.trim(),
        contents: [],
      }),
    onSuccess: (carton) => {
      void queryClient.invalidateQueries({ queryKey: packingKeys.all })
      toast.success('Carton added', `Carton ${carton.carton_number} is open for contents.`)
      setLength('')
      setWidth('')
      setHeight('')
      setTare('')
      setProblem(null)
    },
  })

  const rows = materials.data?.data ?? []

  return (
    <div className="rounded-lg border border-dashed p-3">
      <h4 className="text-sm font-medium">Add a carton</h4>
      <p className="mt-0.5 text-xs text-muted-foreground">
        The box is named now and issued from stock when the carton is sealed.
      </p>

      <div className="mt-3 grid gap-3 sm:grid-cols-3 lg:grid-cols-4">
        <div className="sm:col-span-2">
          <Label htmlFor="carton-material" required>
            Packing material
          </Label>
          <Input
            id="carton-material-search"
            value={materialSearch}
            onChange={(event) => setMaterialSearch(event.target.value)}
            placeholder="Search by code or name"
            autoComplete="off"
            aria-label="Search packing materials"
            className="mt-1.5"
          />
          <Select value={materialId} onValueChange={setMaterialId}>
            <SelectTrigger id="carton-material" className="mt-1.5">
              <SelectValue placeholder="Choose the box being used" />
            </SelectTrigger>
            <SelectContent>
              {rows.map((material) => (
                <SelectItem key={material.item_id} value={material.item_id}>
                  {material.code} - {material.name} ({formatQuantity(material.available_quantity)}{' '}
                  free)
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {!materials.isLoading && rows.length === 0 && (
            <p className="mt-1 text-xs text-warning">
              {debouncedMaterialSearch
                ? `No packing material in stock here matches "${debouncedMaterialSearch}".`
                : 'No packing material is in stock at this warehouse. A carton cannot be sealed without one, because sealing issues the box.'}
            </p>
          )}
          {moreMaterials && (
            <p className="mt-1 text-xs text-muted-foreground">
              Showing the first {LOOKUP_LIST_LIMIT} - search for a box that is not listed.
            </p>
          )}
        </div>

        <div>
          <Label htmlFor="carton-material-quantity">Boxes used</Label>
          <Input
            id="carton-material-quantity"
            value={materialQuantity}
            onChange={(event) => setMaterialQuantity(event.target.value)}
            inputMode="decimal"
            className="mt-1.5 text-right tabular"
          />
        </div>

        <div>
          <Label htmlFor="carton-type" required>
            Type
          </Label>
          <Select value={cartonType} onValueChange={setCartonType}>
            <SelectTrigger id="carton-type" className="mt-1.5" aria-label="Carton type">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {CARTON_TYPES.map((type) => (
                <SelectItem key={type} value={type}>
                  {humanise(type)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div>
          <Label htmlFor="carton-length">Length (cm)</Label>
          <Input
            id="carton-length"
            value={length}
            onChange={(event) => setLength(event.target.value)}
            inputMode="decimal"
            className="mt-1.5"
          />
        </div>
        <div>
          <Label htmlFor="carton-width">Width (cm)</Label>
          <Input
            id="carton-width"
            value={width}
            onChange={(event) => setWidth(event.target.value)}
            inputMode="decimal"
            className="mt-1.5"
          />
        </div>
        <div>
          <Label htmlFor="carton-height">Height (cm)</Label>
          <Input
            id="carton-height"
            value={height}
            onChange={(event) => setHeight(event.target.value)}
            inputMode="decimal"
            className="mt-1.5"
          />
        </div>
        <div>
          <Label htmlFor="carton-tare">Tare weight (kg)</Label>
          <Input
            id="carton-tare"
            value={tare}
            onChange={(event) => setTare(event.target.value)}
            inputMode="decimal"
            className="mt-1.5"
          />
        </div>
      </div>

      {problem && <FieldError message={problem} />}

      <Button
        type="button"
        variant="outline"
        className="mt-3"
        loading={addCarton.isPending}
        onClick={() => {
          if (!materialId) return setProblem('Choose the packing material this box is made of.')
          for (const [label, value] of [
            ['Length', length],
            ['Width', width],
            ['Height', height],
            ['Tare weight', tare],
          ] as const) {
            if (!MEASURE_INPUT.test(value.trim())) {
              return setProblem(`${label} takes up to 3 decimal places.`)
            }
          }
          setProblem(null)
          addCarton.mutate()
        }}
      >
        <Plus />
        Add carton
      </Button>
    </div>
  )
}

// ---------------------------------------------------------- add contents ----

function AddContentsForm({
  order,
  carton,
  lines,
}: {
  order: PackingOrderDetail
  carton: Carton
  lines: PackingOrderLine[]
}) {
  const { can } = useAuth()
  const queryClient = useQueryClient()

  const [lineId, setLineId] = useState('')
  const [quantity, setQuantity] = useState('')
  const [netWeight, setNetWeight] = useState('0')
  const [batchId, setBatchId] = useState('')
  const [serialIds, setSerialIds] = useState<string[]>([])
  const [problem, setProblem] = useState<string | null>(null)

  const selectedLine = lines.find((line) => line.id === lineId) ?? null
  const facts = useItemFacts(selectedLine?.item_id ?? '', can(P.ITEM_VIEW))
  const item = facts.data

  /**
   * The tracking flags decide whether a batch or a serial picker appears, and
   * they come from the item master. The line itself is chosen from the order,
   * so - unlike a screen that picks the item through the item picker - a packer
   * whose role has no `item.view` reaches this form with the flags unknown.
   * Treated as "not tracked", the box would be sent with no serials and the API
   * would refuse it with SERIAL_REQUIRED, which is a message about a field this
   * form never showed. So the unknown is stated and the submit is blocked.
   */
  const trackingUnknown = Boolean(selectedLine) && !facts.isLoading && !item

  useEffect(() => {
    setBatchId('')
    setSerialIds([])
    setProblem(null)
  }, [lineId])

  const replace = useMutation({
    mutationFn: (contents: CartonContentIn[]) =>
      api.put<Carton>(`/packing/${order.id}/cartons/${carton.id}/contents`, {
        reason: 'Packed at the bench',
        contents,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: packingKeys.all })
      toast.success(
        success.updated('Contents'),
        `Carton ${carton.carton_number} has been repacked.`,
      )
      setLineId('')
      setQuantity('')
      setNetWeight('0')
      setBatchId('')
      setSerialIds([])
      setProblem(null)
    },
  })

  const packable = lines.filter((line) => toDecimal(pendingOf(line)).greaterThan(0))

  if (packable.length === 0) {
    return (
      <p className="rounded-md border border-dashed px-3 py-4 text-center text-sm text-muted-foreground">
        Every line on this order is fully packed.
      </p>
    )
  }

  const submit = () => {
    if (!selectedLine) return setProblem('Choose what is going in.')
    if (trackingUnknown) {
      return setProblem(
        'Whether this item is batch or serial tracked cannot be read, so the carton cannot be ' +
          'packed here - reading the item master needs the item.view permission.',
      )
    }
    if (!QUANTITY_INPUT.test(quantity.trim()) || !toDecimal(quantity).greaterThan(0)) {
      return setProblem('Enter how many go in this carton.')
    }
    if (toDecimal(quantity).greaterThan(toDecimal(pendingOf(selectedLine)))) {
      return setProblem(
        `Only ${formatQuantity(pendingOf(selectedLine))} of this line is still unpacked.`,
      )
    }
    if (!MEASURE_INPUT.test(netWeight.trim())) {
      return setProblem('Net weight takes up to 3 decimal places.')
    }
    if (item?.is_batch_tracked && !batchId) {
      return setProblem(`${item.code} is batch tracked - name the batch going in the carton.`)
    }
    if (item?.is_serial_tracked) {
      if (serialIds.length === 0) {
        return setProblem(
          `${item.code} is serial tracked, so every unit in the box must be identified. Pick its serials from the register.`,
        )
      }
      if (!toDecimal(quantity).equals(serialIds.length)) {
        return setProblem(
          `${serialIds.length} serial(s) chosen for a quantity of ${formatQuantity(quantity)} - name exactly one per unit.`,
        )
      }
    }

    // The whole box goes back, not just the new row: `PUT .../contents` replaces
    // the set and gives the previous one back to the order's lines.
    setProblem(null)
    replace.mutate([
      ...toContentInput(carton.contents, lines),
      {
        line_number: selectedLine.line_number,
        quantity: quantity.trim(),
        batch_id: batchId || null,
        serial_ids: item?.is_serial_tracked ? serialIds : [],
        net_weight_kg: netWeight.trim() === '' ? '0' : netWeight.trim(),
      },
    ])
  }

  return (
    <div className="space-y-3 rounded-md border border-dashed p-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div className="sm:col-span-2">
          <Label htmlFor={`contents-line-${carton.id}`} required>
            Item
          </Label>
          <Select value={lineId} onValueChange={setLineId}>
            <SelectTrigger
              id={`contents-line-${carton.id}`}
              className="mt-1.5"
              aria-label="Item to pack"
            >
              <SelectValue placeholder="Choose a line" />
            </SelectTrigger>
            <SelectContent>
              {packable.map((line) => (
                <SelectItem key={line.id} value={line.id}>
                  Line {line.line_number} - {formatQuantity(pendingOf(line))} left
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div>
          <Label htmlFor={`contents-qty-${carton.id}`} required>
            Quantity
          </Label>
          <Input
            id={`contents-qty-${carton.id}`}
            value={quantity}
            onChange={(event) => setQuantity(event.target.value)}
            inputMode="decimal"
            className="mt-1.5 text-right tabular"
          />
        </div>

        <div>
          <Label htmlFor={`contents-weight-${carton.id}`}>Net weight (kg)</Label>
          <Input
            id={`contents-weight-${carton.id}`}
            value={netWeight}
            onChange={(event) => setNetWeight(event.target.value)}
            inputMode="decimal"
            className="mt-1.5 text-right tabular"
          />
        </div>

        {item?.is_batch_tracked && selectedLine && (
          <div className="sm:col-span-2">
            <BatchSelect
              id={`contents-batch-${carton.id}`}
              itemId={selectedLine.item_id}
              value={batchId}
              onChange={setBatchId}
            />
          </div>
        )}
      </div>

      {item?.is_serial_tracked && selectedLine && (
        <SerialPicker
          idPrefix={`carton-${carton.id}`}
          itemId={selectedLine.item_id}
          warehouseId={order.warehouse_id}
          quantity={quantity}
          selected={serialIds}
          onChange={setSerialIds}
        />
      )}

      {trackingUnknown && (
        <Alert variant="warning">
          <AlertTitle>This line&apos;s tracking cannot be read</AlertTitle>
          <AlertDescription>
            Whether the item is batch or serial tracked comes from the item master, and reading it
            needs the item.view permission. Without it a serial-tracked box would be sent with no
            units named and refused, so this line has to be packed by somebody who holds it.
          </AlertDescription>
        </Alert>
      )}

      {problem && <FieldError message={problem} />}

      <Button type="button" size="sm" loading={replace.isPending} onClick={submit}>
        <PackagePlus />
        Pack
      </Button>
    </div>
  )
}

// ----------------------------------------------------------- carton card ----

function CartonLabelPreview({ label }: { label: CartonLabel }) {
  const barcodeRef = useRef<SVGSVGElement>(null)
  const [barcodeError, setBarcodeError] = useState<string | null>(null)
  const dimensions =
    label.length_cm && label.width_cm && label.height_cm
      ? `${label.length_cm} x ${label.width_cm} x ${label.height_cm} cm`
      : null

  useEffect(() => {
    if (label.symbology !== 'CODE128') return
    let active = true
    void import('jsbarcode')
      .then(({ default: renderBarcode }) => {
        if (!active || !barcodeRef.current) return
        renderBarcode(barcodeRef.current, label.barcode, {
          format: 'CODE128',
          displayValue: false,
          height: 42,
          margin: 0,
          width: 1.6,
        })
      })
      .catch((error: unknown) => {
        if (active) {
          setBarcodeError(
            error instanceof Error ? error.message : 'The barcode renderer could not be loaded.',
          )
        }
      })
    return () => {
      active = false
    }
  }, [label.barcode, label.symbology])

  return (
    <section
      className="packing-carton-label-print mx-auto max-w-sm rounded-lg border p-4"
      aria-label="Carton label preview"
    >
      <div className="packing-carton-label-no-print mb-3 flex items-center justify-between gap-2">
        <h4 className="font-medium">Carton label preview</h4>
        <Button type="button" size="sm" onClick={() => window.print()}>
          <Printer />
          Print label
        </Button>
      </div>

      <div className="space-y-2">
        <div className="flex items-start justify-between gap-2">
          <div>
            <p className="text-lg font-bold">{label.customer_name}</p>
            <p className="text-xs">{label.customer_code}</p>
          </div>
          <p className="text-right text-sm font-semibold">
            {label.packing_order_code}
            <span className="block">
              Carton {label.carton_number} of {label.total_cartons}
            </span>
          </p>
        </div>

        <div className="flex justify-center border-y py-2">
          {label.symbology === 'CODE128' && !barcodeError ? (
            <div className="text-center">
              <svg ref={barcodeRef} aria-label="Carton barcode" className="max-w-full" />
              <p className="font-mono text-xs">{label.barcode}</p>
            </div>
          ) : barcodeError ? (
            <Alert variant="destructive">
              <AlertDescription>Could not render barcode: {barcodeError}</AlertDescription>
            </Alert>
          ) : (
            <p role="alert" className="text-sm">
              Barcode symbology {label.symbology} is not supported for browser printing.
            </p>
          )}
        </div>

        <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-sm">
          <span>Type</span>
          <span className="text-right font-medium">{humanise(label.carton_type)}</span>
          <span>Gross weight</span>
          <span className="text-right font-medium">{formatQuantity(label.gross_weight_kg)} kg</span>
          <span>Net weight</span>
          <span className="text-right font-medium">{formatQuantity(label.net_weight_kg)} kg</span>
          {dimensions && (
            <>
              <span>Dimensions</span>
              <span className="text-right font-medium">{dimensions}</span>
            </>
          )}
          {label.sales_order_code && (
            <>
              <span>Sales order</span>
              <span className="text-right font-medium">{label.sales_order_code}</span>
            </>
          )}
        </div>

        {(label.lines ?? []).length > 0 && (
          <div className="border-t pt-2">
            <p className="mb-1 text-xs font-semibold uppercase">Contents</p>
            <ul className="space-y-1 text-xs">
              {(label.lines ?? []).map((line, index) => (
                <li
                  key={`${line.item_id}-${line.batch_number ?? 'batch'}-${index}`}
                  className="flex justify-between gap-2"
                >
                  <span className="min-w-0">
                    <span className="font-medium">{line.item_code}</span>
                    <span className="block truncate">{line.item_name}</span>
                    {line.batch_number && <span>Batch: {line.batch_number}</span>}
                    {line.serial_count > 0 && <span>{line.serial_count} serialized units</span>}
                  </span>
                  <span className="shrink-0 font-medium">{formatQuantity(line.quantity)}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  )
}

function CartonCard({
  order,
  carton,
  lines,
  canManage,
  orderPackable,
}: {
  order: PackingOrderDetail
  carton: Carton
  lines: PackingOrderLine[]
  canManage: boolean
  orderPackable: boolean
}) {
  const queryClient = useQueryClient()
  const [sealOpen, setSealOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [labelOpen, setLabelOpen] = useState(false)

  const label = useQuery({
    queryKey: packingKeys.cartonLabel(order.id, carton.id),
    queryFn: () => api.get<CartonLabel>(`/packing/${order.id}/cartons/${carton.id}/label`),
    enabled: labelOpen,
    staleTime: staleTimes.transactional,
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: packingKeys.all })
    // Sealing issues the box from stock, so balances move with it.
    void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
  }

  const seal = useMutation({
    mutationFn: (reason: string) =>
      api.post(`/packing/${order.id}/cartons/${carton.id}/seal`, { reason }),
    onSuccess: () => {
      invalidate()
      toast.success(
        'Carton sealed',
        `Carton ${carton.carton_number} can no longer be changed, and the box has been issued from stock.`,
      )
      setSealOpen(false)
    },
  })

  const removeCarton = useMutation({
    mutationFn: (reason: string) =>
      api.delete(`/packing/${order.id}/cartons/${carton.id}`, { params: { reason } }),
    onSuccess: () => {
      invalidate()
      toast.success('Carton removed', 'Its contents have gone back to the order lines.')
      setDeleteOpen(false)
    },
  })

  const emptyCarton = useMutation({
    mutationFn: (contents: CartonContentIn[]) =>
      api.put<Carton>(`/packing/${order.id}/cartons/${carton.id}/contents`, {
        reason: 'Content removed at the bench',
        contents,
      }),
    onSuccess: () => {
      invalidate()
      toast.success(success.updated('Contents'), `Carton ${carton.carton_number} has been emptied.`)
    },
  })

  const open = !carton.is_sealed
  const dimensions =
    carton.length_cm && carton.width_cm && carton.height_cm
      ? `${carton.length_cm} x ${carton.width_cm} x ${carton.height_cm} cm`
      : null
  const lineNumberOf = new Map(lines.map((line) => [line.id, line.line_number]))

  return (
    <div className="rounded-lg border">
      <div className="flex flex-wrap items-start justify-between gap-2 border-b bg-muted/30 p-3">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2 font-medium">
            <Package className="h-4 w-4 text-muted-foreground" aria-hidden />
            Carton {carton.carton_number}
            <StatusBadge status={carton.is_sealed ? 'SEALED' : 'OPEN'} />
            <span className="font-mono text-xs text-muted-foreground">{carton.barcode}</span>
          </p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {[
              humanise(carton.carton_type),
              dimensions,
              `Tare ${formatQuantity(carton.tare_weight_kg)} kg`,
              `Net ${formatQuantity(carton.net_weight_kg)} kg`,
              `Gross ${formatQuantity(carton.gross_weight_kg)} kg`,
              carton.sealed_at ? `Sealed ${formatDateTime(carton.sealed_at)}` : null,
            ]
              .filter(Boolean)
              .join(' - ')}
          </p>
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-1">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setLabelOpen((current) => !current)}
            aria-expanded={labelOpen}
          >
            <Printer />
            {labelOpen ? 'Hide label' : 'Print label'}
          </Button>
          {canManage && open && orderPackable && (
            <>
              <Button variant="outline" size="sm" onClick={() => setSealOpen(true)}>
                <Lock />
                Seal carton
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={`Remove carton ${carton.carton_number}`}
                onClick={() => setDeleteOpen(true)}
              >
                <Trash2 className="text-destructive" />
              </Button>
            </>
          )}
        </div>
      </div>

      <div className="p-3">
        {carton.contents.length === 0 ? (
          <p className="py-3 text-center text-sm text-muted-foreground">
            Nothing packed in this carton yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <caption className="sr-only">Contents of carton {carton.carton_number}</caption>
              <thead>
                <tr className="border-b">
                  <th
                    scope="col"
                    className="py-1.5 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                  >
                    Line
                  </th>
                  <th
                    scope="col"
                    className="py-1.5 text-right text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                  >
                    Quantity
                  </th>
                  <th
                    scope="col"
                    className="py-1.5 text-right text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                  >
                    Net kg
                  </th>
                  <th
                    scope="col"
                    className="py-1.5 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                  >
                    Identified
                  </th>
                  {canManage && open && orderPackable && <th scope="col" className="w-8" />}
                </tr>
              </thead>
              <tbody>
                {carton.contents.map((content) => (
                  <tr key={content.id} className="border-b last:border-0">
                    <td className="py-1.5 pr-3">
                      <span className="block font-medium">
                        Line {lineNumberOf.get(content.packing_order_line_id) ?? '?'}
                      </span>
                      <span
                        className="block max-w-[min(18rem,45vw)] truncate font-mono text-xs text-muted-foreground"
                        title={content.item_id}
                      >
                        {content.item_id}
                      </span>
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular">
                      {formatQuantity(content.quantity)}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular">
                      {formatQuantity(content.net_weight_kg)}
                    </td>
                    <td className="py-1.5 text-xs text-muted-foreground">
                      {content.serial_ids.length > 0
                        ? `${content.serial_ids.length} serial${content.serial_ids.length === 1 ? '' : 's'}`
                        : content.batch_id
                          ? 'Batch named'
                          : '-'}
                    </td>
                    {canManage && open && orderPackable && (
                      <td className="py-1.5 text-right">
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          aria-label={`Take content out of carton ${carton.carton_number}`}
                          disabled={emptyCarton.isPending}
                          onClick={() =>
                            emptyCarton.mutate(
                              toContentInput(
                                carton.contents.filter((row) => row.id !== content.id),
                                lines,
                              ),
                            )
                          }
                        >
                          <Trash2 className="text-destructive" />
                        </Button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {canManage && open && orderPackable && (
          <div className="mt-3">
            <AddContentsForm order={order} carton={carton} lines={lines} />
          </div>
        )}

        {labelOpen && (
          <div className="mt-4 border-t pt-4">
            {label.isLoading && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
                <Skeleton className="h-4 w-4 rounded-full" />
                Loading carton label...
              </div>
            )}
            {label.error && (
              <Alert variant="destructive">
                <AlertTitle>Could not load carton label</AlertTitle>
                <AlertDescription>{label.error.message}</AlertDescription>
              </Alert>
            )}
            {label.data && <CartonLabelPreview label={label.data} />}
          </div>
        )}
      </div>

      <ReasonDialog
        open={sealOpen}
        onOpenChange={setSealOpen}
        title={`Seal carton ${carton.carton_number}?`}
        description="A sealed carton is a taped box. Its contents cannot be changed afterwards, and the box itself is issued from stock now - anything still to pack needs another carton."
        confirmLabel="Seal carton"
        loading={seal.isPending}
        onConfirm={(reason) => seal.mutate(reason)}
      />

      <ReasonDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title={`Remove carton ${carton.carton_number}?`}
        description="Whatever is in it goes back to the order's lines. The carton number is not reused."
        confirmLabel="Remove carton"
        destructive
        loading={removeCarton.isPending}
        onConfirm={(reason) => removeCarton.mutate(reason)}
      />
    </div>
  )
}

// --------------------------------------------------------- packing list -----

function PackingListAction({ order }: { order: PackingOrderDetail }) {
  const { can } = useAuth()
  const job = useReportJob()

  const generate = useMutation({
    // A GET that returns 202 with a job id - the render is handed to the report
    // queue rather than held open on this connection.
    mutationFn: () => api.get<AcceptedJobData>(`/packing/${order.id}/packing-list`),
    onSuccess: (accepted) => {
      // The job's status lives in the report module and reading it is its own
      // permission. Without it the build still runs - there is just nothing
      // here that can watch it, so the button is not left spinning forever.
      if (!can(P.REPORT_VIEW)) {
        toast.show(
          'Packing list queued',
          'Watching it needs the report permission, which your role does not have. Ask someone who holds it for the document.',
        )
        return
      }
      job.start(accepted)
    },
    // A failed queue is surfaced by the global mutation cache; only a failed
    // build - a polled query, which that cache does not see - is reported inline.
  })

  return (
    <div className="space-y-2">
      {job.phase === 'idle' && (
        <Button
          variant="outline"
          size="sm"
          onClick={() => generate.mutate()}
          loading={generate.isPending}
          disabled={order.total_cartons === 0}
        >
          <FileText />
          Generate packing list
        </Button>
      )}

      <ReportJobPanel controller={job} label="Packing list" />
    </div>
  )
}

// ------------------------------------------------------- order lifecycle ----

type OrderAction = 'start' | 'complete' | 'hand-over' | 'close' | 'cancel'

/**
 * The order's lifecycle buttons.
 *
 * Packing transitions need PACKING_MANAGE. Hand-over instead needs
 * DISPATCH_CREATE, plus DISPATCH_VIEW to choose the receiving consignment.
 */
function OrderActions({ order }: { order: PackingOrderDetail }) {
  const { can } = useAuth()
  const canManage = can(P.PACKING_MANAGE)
  const queryClient = useQueryClient()
  const [action, setAction] = useState<OrderAction | null>(null)
  const [allowShortPack, setAllowShortPack] = useState(false)
  const [dispatchId, setDispatchId] = useState('')

  // A hand-over names the consignment the boxes are loaded onto, so the only
  // useful list is the customer's consignments that have not left yet.
  const dispatchParams = {
    customer_id: order.customer_id,
    status: 'NOT_DISPATCHED',
    limit: LOOKUP_LIST_LIMIT,
    with_total: false,
  }
  const dispatches = useQuery({
    queryKey: queryKeys.dispatch.list(dispatchParams),
    queryFn: () => api.getPage<DispatchRow>('/dispatch', { params: dispatchParams }),
    enabled: action === 'hand-over',
    staleTime: staleTimes.transactional,
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: packingKeys.all })
    void queryClient.invalidateQueries({ queryKey: queryKeys.dispatch.all })
    void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
  }

  // The confirmation each transition earns, in the past tense; the order's
  // code is the body. Built from a map rather than the action slug, which read
  // as an instruction ("Packing order hand over") rather than a result.
  const done: Record<Exclude<OrderAction, 'complete'>, string> = {
    start: 'Packing started',
    'hand-over': 'Handed to dispatch',
    close: success.closed('Packing order'),
    cancel: success.cancelled('Packing order'),
  }

  const run = useMutation({
    mutationFn: ({ next, reason }: { next: OrderAction; reason: string }) => {
      if (next === 'complete') {
        return api.post<PackingCompletionResult>(`/packing/${order.id}/complete`, {
          reason,
          allow_short_pack: allowShortPack,
        })
      }
      if (next === 'hand-over') {
        return api.post(`/packing/${order.id}/hand-over`, { reason, dispatch_id: dispatchId })
      }
      return api.post(`/packing/${order.id}/${next}`, { reason })
    },
    onSuccess: (result, { next }) => {
      invalidate()
      if (next === 'complete') {
        const completion = result as PackingCompletionResult
        const short = completion.short_packed_line_numbers ?? []
        toast.success(
          success.completed('Packing'),
          short.length > 0
            ? `${completion.total_cartons} carton(s), ${formatQuantity(completion.total_gross_weight)} kg gross. Line(s) ${short.join(', ')} went out short.`
            : `${completion.total_cartons} carton(s), ${formatQuantity(completion.total_gross_weight)} kg gross.`,
        )
      } else {
        toast.success(done[next], order.code)
      }
      setAction(null)
      setAllowShortPack(false)
      setDispatchId('')
    },
  })

  const copy: Record<OrderAction, { title: string; description: string; confirmLabel: string }> = {
    start: {
      title: `Start packing ${order.code}?`,
      description:
        'Cartons can only be opened once the order is in progress - a draft has no bench.',
      confirmLabel: 'Start packing',
    },
    complete: {
      title: `Complete ${order.code}?`,
      description:
        'Every carton must be sealed. The order becomes packed and can be handed to a consignment.',
      confirmLabel: 'Complete packing',
    },
    'hand-over': {
      title: `Hand ${order.code} to a dispatch?`,
      description: 'The cartons are loaded onto the consignment that takes them out.',
      confirmLabel: 'Hand over',
    },
    close: {
      title: `Close ${order.code}?`,
      description: 'The order is finished with. Nothing further can be recorded against it.',
      confirmLabel: 'Close order',
    },
    cancel: {
      title: `Cancel ${order.code}?`,
      description:
        'The order is abandoned. Anything packed goes back to its lines; a sealed box is not unsealed.',
      confirmLabel: 'Cancel order',
    },
  }

  const rows = dispatches.data?.data ?? []
  const moreDispatches = dispatches.data?.pagination?.has_next ?? false

  return (
    <>
      <div className="flex flex-wrap gap-2">
        {canManage && isStartable(order.status) && (
          <Button variant="outline" size="sm" onClick={() => setAction('start')}>
            <Play />
            Start packing
          </Button>
        )}
        {canManage && isCompletable(order.status) && (
          <Button variant="outline" size="sm" onClick={() => setAction('complete')}>
            <PackageCheck />
            Complete
          </Button>
        )}
        {isHandOverable(order.status) && can(P.DISPATCH_CREATE) && can(P.DISPATCH_VIEW) && (
          <Button variant="outline" size="sm" onClick={() => setAction('hand-over')}>
            <Truck />
            Hand to dispatch
          </Button>
        )}
        {canManage && isCloseable(order.status) && (
          <Button variant="outline" size="sm" onClick={() => setAction('close')}>
            Close
          </Button>
        )}
        {canManage && isPackingCancellable(order.status) && (
          <Button
            variant="outline"
            size="sm"
            className="text-destructive"
            onClick={() => setAction('cancel')}
          >
            Cancel order
          </Button>
        )}
      </div>

      <ReasonDialog
        open={action !== null}
        onOpenChange={(open) => {
          if (!open) {
            setAction(null)
            setAllowShortPack(false)
            setDispatchId('')
          }
        }}
        title={action ? copy[action].title : ''}
        description={action ? copy[action].description : undefined}
        confirmLabel={action ? copy[action].confirmLabel : 'Confirm'}
        destructive={action === 'cancel'}
        loading={run.isPending}
        onConfirm={(reason) => {
          if (action === 'hand-over' && !dispatchId) return
          if (action) run.mutate({ next: action, reason })
        }}
      >
        {action === 'complete' && (
          <div className="flex items-start gap-2.5">
            <Checkbox
              id="packing-allow-short"
              checked={allowShortPack}
              onCheckedChange={(checked) => setAllowShortPack(checked === true)}
            />
            <Label htmlFor="packing-allow-short" className="font-normal">
              Allow a short pack - complete even though some lines are not fully packed
            </Label>
          </div>
        )}

        {action === 'hand-over' && (
          <div>
            <Label htmlFor="packing-handover-dispatch" required>
              Consignment
            </Label>
            <Select
              value={dispatchId}
              onValueChange={setDispatchId}
              disabled={dispatches.isLoading || dispatches.isError}
            >
              <SelectTrigger id="packing-handover-dispatch" className="mt-1.5">
                <SelectValue
                  placeholder={
                    dispatches.isLoading
                      ? 'Loading consignments...'
                      : 'Choose the dispatch taking these boxes'
                  }
                />
              </SelectTrigger>
              <SelectContent>
                {rows.map((dispatch) => (
                  <SelectItem key={dispatch.id} value={dispatch.id}>
                    {dispatch.code} - {formatDate(dispatch.dispatch_date)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {dispatches.error && <FieldError message={dispatches.error.message} />}
            {!dispatches.isLoading && !dispatches.error && rows.length === 0 && (
              <FieldError message="This customer has no consignment waiting to be loaded. Create one on the dispatch screen first." />
            )}
            {moreDispatches && (
              <p className="mt-1 text-xs text-warning">
                Showing the first {LOOKUP_LIST_LIMIT} consignments waiting for this customer - an
                older one is not offered here.
              </p>
            )}
          </div>
        )}
      </ReasonDialog>
    </>
  )
}

// ---------------------------------------------------------------- dialog ----

export function PackingOrderDialog({
  order,
  open,
  onOpenChange,
}: {
  order: PackingOrderRow | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { can } = useAuth()

  // Gating is UX only - the API authorises each carton operation itself.
  const canManage = can(P.PACKING_MANAGE)

  const orderId = order?.id ?? ''

  const detail = useQuery({
    queryKey: packingKeys.detail(orderId),
    queryFn: () => api.get<PackingOrderDetail>(`/packing/${orderId}`),
    enabled: open && Boolean(orderId),
    staleTime: staleTimes.transactional,
  })

  // Cartons are their own paged endpoint - they are not part of the order.
  const cartonsQuery = useQuery({
    queryKey: packingKeys.cartons(orderId),
    queryFn: () =>
      api.getPage<Carton>(`/packing/${orderId}/cartons`, {
        params: { limit: CARTON_PAGE, sort_by: 'carton_number', sort_dir: 'asc' },
      }),
    enabled: open && Boolean(orderId),
    staleTime: staleTimes.transactional,
  })

  const data = detail.data
  const cartons = cartonsQuery.data?.data ?? []
  const moreCartons = cartonsQuery.data?.pagination?.has_next ?? false
  const lines = data?.lines ?? []
  const packable = data ? isPackable(data.status) : false
  const sealedCount = cartons.filter((carton) => carton.is_sealed).length

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xl">
        <DialogHeader>
          <DialogTitle>{order ? `Packing ${order.code}` : 'Packing order'}</DialogTitle>
          <DialogDescription>
            Build the cartons for this order, then generate the packing list that travels with the
            consignment.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-5">
          {order && (
            <div className="rounded-lg border bg-muted/30 p-4">
              <DescriptionList
                columns={4}
                items={[
                  { label: 'Status', value: <StatusBadge status={order.status} /> },
                  { label: 'Planned for', value: formatDate(order.planned_date) },
                  { label: 'Packed', value: formatDateTime(order.packed_date) },
                  {
                    label: 'Sales order',
                    value: order.sales_order_id ? 'Against a sales order' : 'Standalone',
                  },
                  {
                    label: 'Handed over',
                    value: order.handed_over_at ? formatDateTime(order.handed_over_at) : '-',
                  },
                ]}
              />
              {order.notes && (
                <p className="mt-3 border-t pt-3 text-sm text-muted-foreground">{order.notes}</p>
              )}
            </div>
          )}

          {detail.isLoading && (
            <div className="space-y-2" aria-hidden>
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-32 w-full" />
              <Skeleton className="h-32 w-full" />
            </div>
          )}

          {!detail.isLoading && detail.error && (
            <Alert variant="destructive">
              <AlertTitle>Could not load this packing order</AlertTitle>
              <AlertDescription>{detail.error.message}</AlertDescription>
            </Alert>
          )}

          {data && (
            <>
              {(canManage || (can(P.DISPATCH_CREATE) && can(P.DISPATCH_VIEW))) && (
                <OrderActions order={data} />
              )}

              {/* ------------------------------------------ running totals -- */}
              <div className="grid gap-3 sm:grid-cols-3">
                <div className="rounded-lg border p-3">
                  <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    <Boxes className="h-3.5 w-3.5" aria-hidden />
                    Cartons
                  </p>
                  <p className="mt-1 text-2xl font-semibold tabular">
                    {formatNumber(data.total_cartons)}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {formatNumber(sealedCount)} sealed, {formatNumber(cartons.length - sealedCount)}{' '}
                    open
                  </p>
                </div>

                <div className="rounded-lg border p-3">
                  <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    <Weight className="h-3.5 w-3.5" aria-hidden />
                    Gross weight
                  </p>
                  {/* The API's own running totals, not a browser-side sum of the
                      carton page - the freight is priced on these. */}
                  <p className="mt-1 text-2xl font-semibold tabular">
                    {formatQuantity(data.total_gross_weight)} kg
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Net {formatQuantity(data.total_net_weight)} kg
                  </p>
                </div>

                <div className="rounded-lg border p-3">
                  <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    <Package className="h-3.5 w-3.5" aria-hidden />
                    Lines
                  </p>
                  <p className="mt-1 text-2xl font-semibold tabular">
                    {formatNumber(lines.length)}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {formatNumber(
                      lines.filter((line) => toDecimal(pendingOf(line)).greaterThan(0)).length,
                    )}{' '}
                    still to pack
                  </p>
                </div>
              </div>

              {/* -------------------------------------------------- lines --- */}
              <div>
                <h3 className="mb-2 text-sm font-medium">Lines</h3>
                <div className="overflow-x-auto rounded-lg border">
                  <table className="w-full text-sm">
                    <caption className="sr-only">Lines on this packing order</caption>
                    <thead className="bg-muted/50">
                      <tr>
                        <th
                          scope="col"
                          className="h-9 px-3 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                        >
                          Line
                        </th>
                        <th
                          scope="col"
                          className="h-9 px-3 text-right text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                        >
                          To pack
                        </th>
                        <th
                          scope="col"
                          className="h-9 px-3 text-right text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                        >
                          Packed
                        </th>
                        <th
                          scope="col"
                          className="h-9 px-3 text-right text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                        >
                          Still to pack
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {lines.map((line) => {
                        const pending = pendingOf(line)
                        return (
                          <tr key={line.id} className="border-t">
                            <td className="px-3 py-2">
                              <span className="block font-medium">Line {line.line_number}</span>
                              <span
                                className="block max-w-[min(18rem,45vw)] truncate font-mono text-xs text-muted-foreground"
                                title={line.item_id}
                              >
                                {line.item_id}
                              </span>
                            </td>
                            <td className="px-3 py-2 text-right tabular">
                              {formatQuantity(line.quantity_to_pack)}
                            </td>
                            <td className="px-3 py-2 text-right tabular">
                              {formatQuantity(line.packed_quantity)}
                            </td>
                            <td className="px-3 py-2 text-right tabular">
                              {toDecimal(pending).isZero() ? (
                                <span className="text-success">Complete</span>
                              ) : (
                                <span className="font-medium text-warning">
                                  {formatQuantity(pending)}
                                </span>
                              )}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* ------------------------------------------------ cartons --- */}
              <div className="space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-sm font-medium">Cartons</h3>
                  <PackingListAction order={data} />
                </div>

                {!packable && (
                  <Alert variant="info">
                    <AlertTitle>
                      {isStartable(data.status)
                        ? 'Packing has not started'
                        : 'This order is no longer being packed'}
                    </AlertTitle>
                    <AlertDescription>
                      {isStartable(data.status)
                        ? 'Cartons may only be opened once the order is in progress. Start it above.'
                        : `It is ${humanise(data.status).toLowerCase()}, so its cartons are shown as a record and cannot be changed.`}
                    </AlertDescription>
                  </Alert>
                )}

                {moreCartons && (
                  <Alert variant="warning">
                    <AlertTitle>Showing the first {CARTON_PAGE} cartons</AlertTitle>
                    <AlertDescription>
                      This order has more boxes than one page. The totals above are the API&apos;s
                      own and cover all of them.
                    </AlertDescription>
                  </Alert>
                )}

                {cartonsQuery.isLoading ? (
                  <Skeleton className="h-32 w-full" />
                ) : cartons.length === 0 ? (
                  <EmptyState
                    icon={Boxes}
                    title="No cartons yet"
                    description="Add a carton, then pack lines into it."
                  />
                ) : (
                  cartons.map((carton) => (
                    <CartonCard
                      key={carton.id}
                      order={data}
                      carton={carton}
                      lines={lines}
                      canManage={canManage}
                      orderPackable={packable}
                    />
                  ))
                )}

                {canManage && packable && <AddCartonForm order={data} />}
              </div>
            </>
          )}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

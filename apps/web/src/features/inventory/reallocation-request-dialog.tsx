/**
 * Raise a reallocation request from any shortfall screen.
 *
 * The reservations screen was the only place this dialog existed, which meant a
 * planner looking at an order line that is short - or at the MRP shortage behind
 * it - had to leave the screen, find the reservation, and retype the quantity.
 * The server contract never needed that: a reallocation names the demand being
 * *helped* (item, warehouse, demand, quantity) and the engine ranks the donors.
 * This component is that contract in one place, so the sales order panel, the
 * reservations table and the MRP shortage report all raise the same request with
 * the same wording, and the engine owns the donor choice in every case.
 *
 * There is no preview step, and there never was one on the server: ranking the
 * donors and raising the request are a single call, because ranking them first
 * and asking the browser to post the winner back would leave a window in which
 * that reservation is taken by somebody else - and would let a client name a
 * donor the engine never offered. So this dialog asks for the shortfall and the
 * reason (and a warehouse when the caller does not already know one), sends one
 * request, and then shows what the engine decided and what it turned down.
 *
 * Nothing moves either way until an approver acts on the PENDING request.
 */

import { joinValidationMessages } from '@/lib/messages'

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link } from 'react-router-dom'
import { z } from 'zod'

import { FieldError, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Input,
  Label,
  Textarea,
} from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { REASON_MAX_LENGTH } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { formatDate, formatQuantity, humanise, toDecimal } from '@/lib/format'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { DEMAND_ROUTES } from './demand-routes'
import { WarehouseSelect } from './pickers'
import { type DemandType, type ReallocationRequest } from './types'

const reallocationSchema = z.object({
  // Asked for rather than assumed. A reservation row's `remaining_quantity` is
  // what that demand already *holds*, not what it is short of - claiming that
  // much again would leave the demand holding twice the stock once an approver
  // signs it, because executing the request opens a second reservation. A
  // shortfall row carries the real short figure and pre-fills it.
  quantity: z
    .string()
    .trim()
    .refine(
      (value) => /^\d+(\.\d{1,4})?$/.test(value),
      'Use a positive quantity with up to 4 decimal places.',
    )
    .refine((value) => toDecimal(value).greaterThan(0), 'Enter an amount greater than zero.'),
  reason: z
    .string()
    .trim()
    .min(5, 'Explain why this demand should take priority over the one losing stock.')
    .max(
      REASON_MAX_LENGTH,
      `Keep the reason under ${REASON_MAX_LENGTH} characters - the API stores no more.`,
    ),
})

type ReallocationForm = z.infer<typeof reallocationSchema>

/** The demand a reallocation request is raised for. */
export interface ReallocationDemand {
  itemId: string
  itemCode?: string | null
  itemName?: string | null
  /**
   * The warehouse whose stock the recipient should be given. Fixed when the
   * caller is a document that already names one - a sales order, a run scoped to
   * a single warehouse - and chosen in the form when the caller does not.
   */
  warehouseId?: string | null
  warehouseCode?: string | null
  demandType: DemandType
  demandId: string
  /** Required for a sales-order demand; the API refuses one without it. */
  demandLineId?: string | null
  demandCode?: string | null
  priority?: number | null
  requiredDate?: string | null
  /** The known short quantity, pre-filled when the screen has it. */
  shortfallQuantity?: string | null
  /** What the demand already holds, for the helper copy only. */
  alreadyHolding?: string | null
}

function positiveQuantity(value: string | null | undefined): string {
  if (!value) return ''
  return toDecimal(value).greaterThan(0) ? value : ''
}

export function ReallocationRequestDialog({
  demand,
  onClose,
}: {
  demand: ReallocationDemand
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [outcome, setOutcome] = useState<ReallocationRequest | null>(null)
  const [warehouseId, setWarehouseId] = useState(demand.warehouseId ?? '')
  const [warehouseError, setWarehouseError] = useState<string | null>(null)

  const defaults = useMemo<ReallocationForm>(
    () => ({ quantity: positiveQuantity(demand.shortfallQuantity), reason: '' }),
    [demand.shortfallQuantity],
  )

  const form = useForm<ReallocationForm>({
    resolver: zodResolver(reallocationSchema),
    defaultValues: defaults,
  })

  const raise = useMutation({
    mutationFn: (values: ReallocationForm) =>
      api.post<ReallocationRequest>('/allocation/reallocation-requests', {
        item_id: demand.itemId,
        warehouse_id: warehouseId,
        // What this demand is short of, as typed - a decimal string all the way
        // to the wire, never parsed into a double. The engine nets free stock
        // off it before it ranks a single donor.
        quantity: values.quantity.trim(),
        to_demand_type: demand.demandType,
        to_demand_id: demand.demandId,
        to_demand_line_id: demand.demandLineId ?? null,
        to_demand_code: demand.demandCode ?? null,
        ...(demand.priority != null ? { priority: demand.priority } : {}),
        required_date: demand.requiredDate ?? null,
        reason: values.reason.trim(),
      }),
    onSuccess: (result) => {
      setOutcome(result)
      toast.success(
        'Reallocation requested',
        formatQuantity(result.quantity) +
          ' claimed from ' +
          (result.from_demand_code ?? humanise(result.from_demand_type ?? 'the donor')) +
          '. The request is ' +
          humanise(result.status) +
          '.',
      )
      void queryClient.invalidateQueries({ queryKey: queryKeys.allocation.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.inventory.all })
    },
    onError: (error) => applyFieldErrors(error, form.setError),
  })

  const chosen = outcome?.candidates.find(
    (candidate) => candidate.reservation_id === outcome.from_reservation_id,
  )
  const alternatives =
    outcome?.candidates.filter(
      (candidate) => candidate.reservation_id !== outcome.from_reservation_id,
    ) ?? []

  const itemLabel = demand.itemCode ?? demand.itemName ?? 'this item'
  const fixedWarehouse = Boolean(demand.warehouseId)

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{outcome ? 'Reallocation requested' : 'Suggest a reallocation'}</DialogTitle>
          <DialogDescription>
            {outcome ? (
              <>
                The allocation engine chose the donor and raised the request. Nothing has moved: an
                approver has to sign it first.
              </>
            ) : (
              <>
                The engine ranks the reservations that could give up {itemLabel}
                {demand.warehouseCode ? ` at ${demand.warehouseCode}` : ''} and raises the request
                against the best one. Say how much <em>more</em> this demand needs; free stock is
                netted off before any donor is considered.
              </>
            )}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {!outcome && (
            <>
              <Alert>
                <AlertTitle>Who gives the stock up is decided by the engine</AlertTitle>
                <AlertDescription>
                  Donors are ranked on priority, required date and how much of the claim is still
                  unconsumed. Scoring it in the browser would mean guessing at priorities the server
                  owns, and the ranking has to be taken under a lock, or two planners promise
                  themselves the same units.
                </AlertDescription>
              </Alert>

              {!fixedWarehouse && (
                <WarehouseSelect
                  id="reallocation-warehouse"
                  label="Warehouse holding the stock"
                  value={warehouseId}
                  onChange={(next) => {
                    setWarehouseId(next)
                    setWarehouseError(null)
                  }}
                  invalid={Boolean(warehouseError)}
                />
              )}
              {warehouseError && <FieldError message={warehouseError} />}

              <div className="space-y-1.5">
                <Label htmlFor="reallocation-quantity" required>
                  Quantity still needed
                </Label>
                {/* A text input, not `type="number"`: the browser normalises a
                    numeric field through a double, and a 4-decimal quantity
                    does not survive that round trip. */}
                <Input
                  id="reallocation-quantity"
                  autoFocus
                  inputMode="decimal"
                  autoComplete="off"
                  placeholder="0"
                  className="max-w-[12rem] text-right tabular"
                  aria-invalid={Boolean(form.formState.errors.quantity)}
                  aria-describedby="reallocation-quantity-hint"
                  {...form.register('quantity')}
                />
                <p id="reallocation-quantity-hint" className="text-xs text-muted-foreground">
                  {demand.alreadyHolding
                    ? `On top of the ${formatQuantity(demand.alreadyHolding)} this demand is already holding. `
                    : ''}
                  Free stock at the position is netted off before any donor is considered.
                </p>
                <FieldError message={form.formState.errors.quantity?.message} />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="reallocation-reason" required>
                  Reason
                </Label>
                <Textarea
                  id="reallocation-reason"
                  placeholder="e.g. Customer escalation - SO/2026-27/00184 ships this week"
                  aria-invalid={Boolean(form.formState.errors.reason)}
                  {...form.register('reason')}
                />
                <FieldError message={form.formState.errors.reason?.message} />
              </div>
            </>
          )}

          {outcome && (
            <>
              <div className="rounded-md border border-primary/50 bg-primary/5 p-3">
                <p className="text-sm font-medium">
                  Taking {formatQuantity(outcome.quantity)} from{' '}
                  {outcome.from_demand_code ??
                    humanise(outcome.from_demand_type ?? 'another reservation')}
                </p>
                <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  {outcome.from_reservation_type && (
                    <Badge
                      variant={outcome.from_reservation_type === 'HARD' ? 'warning' : 'secondary'}
                    >
                      {outcome.from_reservation_type}
                    </Badge>
                  )}
                  <StatusBadge status={outcome.status} />
                </p>
                {chosen && chosen.reasons.length > 0 && (
                  <ul className="mt-2 list-inside list-disc text-xs text-muted-foreground">
                    {chosen.reasons.map((why) => (
                      <li key={why}>{why}</li>
                    ))}
                  </ul>
                )}
                {chosen?.requires_approval && (
                  <p className="mt-2 text-xs text-warning">
                    This donor holds a hard reservation, so the request waits for an allocation
                    approver before any stock moves.
                  </p>
                )}
              </div>

              {alternatives.length > 0 ? (
                <div>
                  <p className="mb-2 text-sm font-medium">
                    Also considered ({alternatives.length})
                  </p>
                  <ul className="space-y-2">
                    {alternatives.map((candidate) => {
                      const route = DEMAND_ROUTES[candidate.demand_type]
                      return (
                        <li
                          key={candidate.reservation_id}
                          className="flex items-start justify-between gap-3 rounded-md border p-3"
                        >
                          <div className="min-w-0">
                            <p className="flex flex-wrap items-center gap-2 text-sm">
                              <span className="font-medium">
                                {candidate.demand_code ?? humanise(candidate.demand_type)}
                              </span>
                              <Badge
                                variant={
                                  candidate.reservation_type === 'HARD' ? 'warning' : 'secondary'
                                }
                              >
                                {candidate.reservation_type}
                              </Badge>
                              <span className="text-xs text-muted-foreground">
                                Priority {candidate.priority}
                              </span>
                            </p>
                            {candidate.reasons.length > 0 && (
                              <p className="mt-1 text-xs text-muted-foreground">
                                {joinValidationMessages(candidate.reasons)}
                              </p>
                            )}
                            {route && (
                              <Link
                                to={route(candidate.demand_id)}
                                className="mt-1 inline-block text-xs text-primary hover:underline"
                              >
                                Open the demand
                              </Link>
                            )}
                          </div>
                          <div className="shrink-0 text-right">
                            <p className="tabular font-medium">
                              {formatQuantity(candidate.takeable_quantity)}
                            </p>
                            <p className="text-xs text-muted-foreground">could give up</p>
                            <p className="text-xs text-muted-foreground">
                              {formatDate(candidate.required_date)}
                            </p>
                          </div>
                        </li>
                      )
                    })}
                  </ul>
                </div>
              ) : (
                <Alert variant="warning">
                  <AlertTitle>No alternative donor</AlertTitle>
                  <AlertDescription>
                    Nothing else holding this item at this warehouse could have given stock up. If
                    this request is rejected, the shortage has to be covered by a purchase, a
                    production order or a transfer.
                  </AlertDescription>
                </Alert>
              )}
            </>
          )}

          <FieldError message={form.formState.errors.root?.message} />
        </DialogBody>

        <DialogFooter>
          {outcome ? (
            <Button onClick={onClose}>Done</Button>
          ) : (
            <>
              <Button variant="outline" onClick={onClose} disabled={raise.isPending}>
                Cancel
              </Button>
              <Button
                onClick={(event) => {
                  if (!warehouseId) {
                    setWarehouseError('Choose the warehouse that holds the stock.')
                    return
                  }
                  void form.handleSubmit((values) => raise.mutate(values))(event)
                }}
                loading={raise.isPending}
              >
                Raise reallocation request
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

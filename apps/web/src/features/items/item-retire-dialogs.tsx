/**
 * Retiring an item, and retiring one of its variants.
 *
 * `DELETE /products/items/{id}` and `DELETE /products/variants/{id}` are both
 * soft deletes, and both are gated on `item.delete` - not on `item.update`,
 * which is the neighbouring right that governs deactivating an item. The
 * difference is the point: deactivation is reversible and retirement is not.
 *
 * Four properties of these two endpoints shape everything below.
 *
 * 1. **Neither accepts a reason.** No body, no query parameter - the contract
 *    declares nothing but the path id, and the service writes the audit row
 *    with its own wording ("Item retired", "Retired variant X") against the
 *    acting user. So there is no reason box here, unlike
 *    `customers/customer-retire-dialog.tsx` where the API demands one. Adding
 *    one would be a field the server never reads.
 * 2. **Nothing is erased.** `soft_delete` stamps `deleted_at`; the row stays so
 *    that ledger entries, BOM lines and posted documents keep resolving its
 *    code and name (section 28). Every read this application makes filters it
 *    out, which is a stronger statement than it sounds: `GET
 *    /products/items/{id}` then answers 404, so the page the user retired from
 *    stops resolving and this dialog navigates away rather than leaving them
 *    looking at an error.
 * 3. **Retirement is one-way.** ARCHIVED is terminal in `ITEM_LIFECYCLE` and no
 *    endpoint reverses it, so there is no undo to offer and the dialog says so
 *    instead of implying one.
 * 4. **The service refuses nothing else.** `delete_item` asserts the lifecycle
 *    transition and stops there - it does not check stock, where-used or
 *    transaction history. An item sitting on the shelf and used in an approved
 *    BOM retires successfully, and the balances and BOM lines are left exactly
 *    where they were. That is the one thing a user cannot see coming, so the
 *    dialog goes and looks: it reads the stock summary and the where-used list
 *    and reports what will be left behind. Those reads warn; they never block,
 *    because the API does not.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'

import { ConfirmDialog } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { mastersKeys } from '@/features/masters/masters-model'
import { api } from '@/lib/api-client'
import { formatQuantity, toDecimal } from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import type { ItemDetail, ItemStockSummary, ItemVariant, WhereUsedRow } from './item-model'

/**
 * The consequence checks want a yes/no, not a page. One row is enough to know
 * the list is non-empty, and `with_total` is on by default so the exact count
 * arrives with it.
 */
const CHECK_LIMIT = 1

/** Plural that reads correctly at one, and admits when the count is unknown. */
function bomPhrase(total: number | null | undefined): string {
  if (total === null || total === undefined) return 'at least one bill of materials'
  return total === 1 ? '1 bill of materials' : `${total} bills of materials`
}

export function RetireItemDialog({
  item,
  uomCode,
  open,
  onOpenChange,
}: {
  item: ItemDetail
  /** Base unit code, so the quantity left behind is stated in real units. */
  uomCode: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { can } = useAuth()

  // Both checks are separate permissions from `item.delete`. A principal who
  // may retire an item need not be able to read stock or bills of materials, so
  // each is asked for only when it would be answered rather than earning a 403
  // the user can do nothing about.
  const canCheckStock = can(P.INVENTORY_VIEW)
  const canCheckBoms = can(P.BOM_VIEW)

  // A distinct key suffix: the Stock tab's own query is paged and keyed by page
  // number, and this one asks a different question of the same endpoint. Both
  // sit under the same prefix, so one invalidation still reaches both.
  const stock = useQuery({
    queryKey: [...queryKeys.inventory.itemStock(item.id), 'retire-check'],
    queryFn: async () =>
      (
        await api.getPage<ItemStockSummary>(`/inventory/items/${item.id}/summary`, {
          // The totals on the payload cover the whole item; `limit` only trims
          // the per-warehouse breakdown, which this dialog does not show.
          params: { limit: CHECK_LIMIT },
        })
      ).data[0] ?? null,
    // Availability is never served stale (master rules 34), and a figure that
    // decides whether a warning appears is the last one to cache.
    staleTime: staleTimes.live,
    enabled: open && canCheckStock,
  })

  const whereUsed = useQuery({
    queryKey: [...queryKeys.bom.whereUsed(item.id), 'retire-check'],
    queryFn: () =>
      api.getPage<WhereUsedRow>(`/bom/where-used/${item.id}`, { params: { limit: CHECK_LIMIT } }),
    staleTime: staleTimes.master,
    enabled: open && canCheckBoms,
  })

  // Owned, not on-hand: stock in transit and stock at a job worker is equally
  // left behind, and it is the quantity people forget. Compared as a decimal
  // rather than with `> 0` - a negative balance is also non-zero, and is also
  // something to say out loud before the master row goes.
  const owned = toDecimal(stock.data?.total_owned_quantity)
  const hasStock = !owned.isZero()
  const usedInBoms = (whereUsed.data?.data.length ?? 0) > 0
  const checksSkipped = !canCheckStock || !canCheckBoms

  const retire = useMutation({
    // No body and no `params`: the contract declares neither on this DELETE.
    mutationFn: () => api.delete(`/products/items/${item.id}`),
    onSuccess: () => {
      // One prefix invalidation covers the list, the detail and every item
      // picker in the application - they all hang off `queryKeys.items`.
      void queryClient.invalidateQueries({ queryKey: queryKeys.items.all })
      toast.success(success.retired('Item'), `${item.code} - ${item.name}`)
      onOpenChange(false)
      // The detail route reads `GET /products/items/{id}`, which now answers
      // 404. Replacing the history entry keeps Back from returning to a page
      // that can only fail.
      navigate('/items', { replace: true })
    },
  })

  // Reset on close, not on open: a refused attempt stays readable while the
  // dialog is up instead of vanishing under a toast.
  const { reset: clearResult } = retire
  useEffect(() => {
    if (!open) clearResult()
  }, [open, clearResult])

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Retire ${item.code}?`}
      description={
        <>
          <strong className="font-medium text-foreground">{item.name}</strong> stops appearing on
          every item list, picker and lookup, and this page stops resolving. Ledger entries, BOM
          lines and documents already issued keep their reference to it - nothing is erased.
        </>
      }
      confirmLabel="Retire item"
      destructive
      loading={retire.isPending}
      onConfirm={() => retire.mutate()}
    >
      <div className="space-y-3">
        {retire.error && (
          <Alert variant="destructive">
            <AlertTitle>The item was not retired</AlertTitle>
            {/* The API's own sentence. It names the state that stood in the way,
                which is more use than "request failed". */}
            <AlertDescription>{retire.error.message}</AlertDescription>
          </Alert>
        )}

        {(hasStock || usedInBoms) && (
          <Alert variant="warning">
            <AlertTitle>This item is still in use</AlertTitle>
            <AlertDescription>
              <ul className="list-disc space-y-1 pl-4">
                {hasStock && (
                  <li>
                    {formatQuantity(owned.toFixed(), uomCode)} is owned - on the shelf, in transit
                    or at a job worker. Retiring the item moves, writes off and revalues none of it;
                    the balances stay exactly where they are and stop being reachable from the item
                    master.
                  </li>
                )}
                {usedInBoms && (
                  <li>
                    It is a component in {bomPhrase(whereUsed.data?.pagination.total)}. Those lines
                    keep pointing at it, so an explosion still resolves its code - but nobody will
                    be able to find the item to maintain them.
                  </li>
                )}
              </ul>
              <p className="mt-2">
                The API refuses neither. It will retire the item anyway, so this is a decision, not
                a warning it will enforce.
              </p>
            </AlertDescription>
          </Alert>
        )}

        <p className="text-xs text-muted-foreground">
          Retirement is final: the archived state is terminal and no screen or endpoint reverses it.
          The audit trail records the status change against your account - this endpoint takes no
          reason, so there is nothing to type.
          {checksSkipped &&
            ' Stock and bill-of-materials use could not be checked from this account.'}
        </p>
      </div>
    </ConfirmDialog>
  )
}

export function RetireVariantDialog({
  variant,
  itemId,
  open,
  onOpenChange,
}: {
  /** The variant being retired; null while the dialog is closed. */
  variant: ItemVariant | null
  /** Parent item, for the two caches keyed by it. */
  itemId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()

  const retire = useMutation({
    mutationFn: (target: ItemVariant) => api.delete(`/products/variants/${target.id}`),
    onSuccess: (_result, target) => {
      // The variants panel is keyed under the item's detail prefix.
      void queryClient.invalidateQueries({ queryKey: queryKeys.items.detail(itemId) })
      // The price list screen names its variant picker under its own key, so
      // the items prefix does not reach it. A retired variant left in that
      // cache is an option whose every use now answers 404 - the same trap the
      // dealer list sprang in `customers/customer-retire-dialog.tsx`.
      void queryClient.invalidateQueries({ queryKey: mastersKeys.variants(itemId) })
      toast.success(success.retired('Variant'), `${target.code} - ${target.name}`)
      onOpenChange(false)
    },
  })

  const { reset: clearResult } = retire
  useEffect(() => {
    if (!open) clearResult()
  }, [open, clearResult])

  if (!variant) return null

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Retire ${variant.code}?`}
      description={
        <>
          <strong className="font-medium text-foreground">{variant.name}</strong> stops appearing in
          the variant list and in the price list&apos;s variant picker. Prices, sales lines and
          stock already recorded against it keep their reference - nothing is erased, and the parent
          item is untouched.
        </>
      }
      confirmLabel="Retire variant"
      destructive
      loading={retire.isPending}
      onConfirm={() => retire.mutate(variant)}
    >
      <div className="space-y-3">
        {retire.error && (
          <Alert variant="destructive">
            <AlertTitle>The variant was not retired</AlertTitle>
            <AlertDescription>{retire.error.message}</AlertDescription>
          </Alert>
        )}

        <p className="text-xs text-muted-foreground">
          Retirement is final - no screen brings a variant back. Its finish, size and colour
          combination is freed the moment it goes, so an identical variant can be created again
          under a new code. This endpoint takes no reason; the audit trail records the removal
          against your account.
        </p>
      </div>
    </ConfirmDialog>
  )
}

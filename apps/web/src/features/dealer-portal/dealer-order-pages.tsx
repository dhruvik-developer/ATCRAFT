import type { ColumnDef } from '@tanstack/react-table'
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import {
  ConfirmDialog,
  DescriptionList,
  EmptyState,
  FieldError,
  PageHeader,
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
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  Card,
  CardContent,
  Input,
  Label,
  Textarea,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { api, ApiError } from '@/lib/api-client'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import { formatDate, formatMoney, formatQuantity, toDateInput } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

import { DEALER_API, dealerError, useDealerAction, useDealerRead } from './dealer-hooks'
import { dealerCheckoutPayload, dealerQuantity } from './dealer-payloads'
import { DealerCredit, DealerResult } from './dealer-shared'

function CartQuantity({ line }: { line: SchemaOf<'DealerCartLineRead'> }) {
  const [quantity, setQuantity] = useState(line.quantity)
  const [error, setError] = useState<string>()
  const action = useDealerAction(
    (value: string) => api.patch(`${DEALER_API}/cart/lines/${line.id}`, { quantity: value }),
    'Cart updated',
  )
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        try {
          setError(undefined)
          action.mutate(dealerQuantity(quantity))
        } catch (failure) {
          setError((failure as Error).message)
        }
      }}
      className="space-y-2"
    >
      <Label htmlFor={`cart-${line.id}`}>Quantity for {line.item_code}</Label>
      <div className="flex gap-2">
        <Input
          id={`cart-${line.id}`}
          inputMode="decimal"
          className="min-h-11 w-28"
          value={quantity}
          onChange={(event) => setQuantity(event.target.value)}
        />
        <Button
          variant="outline"
          type="submit"
          className="min-h-11"
          disabled={action.isPending || quantity === line.quantity}
        >
          Update
        </Button>
      </div>
      <FieldError message={error || (action.error ? dealerError(action.error) : undefined)} />
    </form>
  )
}

export function DealerCartPage() {
  const navigate = useNavigate()
  const cart = useDealerRead<SchemaOf<'DealerCartRead'>>('cart')
  const me = useDealerRead<SchemaOf<'DealerPortalMe'>>('me')
  const [checkoutOpen, setCheckoutOpen] = useState(false)
  const [staleCart, setStaleCart] = useState(false)
  const [formError, setFormError] = useState<string>()
  const remove = useDealerAction(
    (id: string) => api.delete(`${DEALER_API}/cart/lines/${id}`),
    'Removed from the cart',
  )
  const checkout = useDealerAction(
    (payload: SchemaOf<'DealerCheckoutRequest'>) =>
      api.post<SchemaOf<'DealerOrderDetail'>>(`${DEALER_API}/checkout`, payload),
    'Order placed for review',
    (order) => navigate(`/dealer/orders/${order.id}`),
    (error) => {
      if (error instanceof ApiError && error.status === 409) {
        setStaleCart(true)
        void cart.refetch()
      }
    },
  )
  const data = cart.data
  const closeCheckout = () => {
    setCheckoutOpen(false)
    setStaleCart(false)
    checkout.reset()
  }
  return (
    <>
      <PageHeader
        title="Cart"
        description="Prices, offers, taxes and credit are checked again when you place your order."
      />
      <DealerResult loading={cart.isLoading} error={cart.error}>
        {data &&
          (data.lines.length ? (
            <div className="space-y-5">
              {data.lines.map((line) => (
                <Card key={line.id}>
                  <CardContent className="grid gap-4 p-4 sm:grid-cols-2">
                    <div>
                      <Link
                        to={`/dealer/catalog/${line.item_id}`}
                        className="font-medium underline"
                      >
                        {line.item_name}
                      </Link>
                      <p className="mt-1 text-xs text-muted-foreground">{line.item_code}</p>
                      <p className="mt-2">
                        {line.price_on_request ? 'Price on request' : formatMoney(line.unit_price)}{' '}
                        / unit
                      </p>
                      {line.offer && (
                        <p className="text-sm text-success">
                          {line.offer.title} · {formatQuantity(line.discount_percentage)}% off
                        </p>
                      )}
                    </div>
                    <div className="space-y-2">
                      <CartQuantity key={`${line.id}-${line.quantity}`} line={line} />
                      <p className="text-sm">
                        Tax {formatMoney(line.tax_amount)} · Total {formatMoney(line.line_total)}
                      </p>
                      <Button
                        variant="outline"
                        className="min-h-11"
                        disabled={remove.isPending}
                        onClick={() => remove.mutate(line.id)}
                      >
                        Remove {line.item_code}
                      </Button>
                    </div>
                  </CardContent>
                </Card>
              ))}
              <DescriptionList
                items={[
                  { label: 'Subtotal', value: formatMoney(data.subtotal) },
                  { label: 'Discount', value: formatMoney(data.discount_amount) },
                  { label: 'Tax', value: formatMoney(data.tax_amount) },
                  { label: 'Total', value: formatMoney(data.total_amount) },
                ]}
              />
              {data.warnings.length > 0 && (
                <Alert>
                  <AlertDescription>
                    <ul className="list-disc space-y-1 pl-5">
                      {data.warnings.map((warning, index) => (
                        <li key={`${warning.code}-${index}`}>{warning.message}</li>
                      ))}
                    </ul>
                  </AlertDescription>
                </Alert>
              )}
              <DealerCredit credit={data.credit} />
              <Button
                className="min-h-11"
                onClick={() => setCheckoutOpen(true)}
                disabled={!data.cart_version}
              >
                Place order for review
              </Button>
            </div>
          ) : (
            <EmptyState
              title="Your cart is empty"
              description="Browse the catalog to add products."
              action={
                <Button asChild>
                  <Link to="/dealer/catalog">Browse catalog</Link>
                </Button>
              }
            />
          ))}
      </DealerResult>
      <Dialog
        open={checkoutOpen}
        onOpenChange={(open) => {
          setCheckoutOpen(open)
          if (!open) {
            setStaleCart(false)
            checkout.reset()
          }
        }}
      >
        <DialogContent>
          <form
            onSubmit={(event) => {
              event.preventDefault()
              try {
                setFormError(undefined)
                checkout.mutate(
                  dealerCheckoutPayload(
                    data?.cart_version ?? null,
                    new FormData(event.currentTarget),
                  ),
                )
              } catch (failure) {
                setFormError((failure as Error).message)
              }
            }}
          >
            <DialogHeader>
              <DialogTitle>Place order for review</DialogTitle>
              <DialogDescription>
                Your sales team will review availability and confirm the order.
              </DialogDescription>
            </DialogHeader>
            <DialogBody className="space-y-4">
              <div>
                <Label htmlFor="checkout-date">Required date</Label>
                <Input
                  id="checkout-date"
                  name="required_date"
                  type="date"
                  required
                  min={toDateInput(new Date())}
                  className="mt-1 min-h-11"
                />
              </div>
              {(['shipping', 'billing'] as const).map((kind) => (
                <div key={kind}>
                  <Label htmlFor={`checkout-${kind}`}>
                    {kind === 'shipping' ? 'Ship to' : 'Bill to'}
                  </Label>
                  <select
                    id={`checkout-${kind}`}
                    name={`${kind}_address_id`}
                    className="mt-1 min-h-11 w-full rounded-md border bg-background px-3 text-sm"
                  >
                    <option value="">Use default address</option>
                    {me.data?.addresses.map((address) => (
                      <option key={address.id} value={address.id}>
                        {address.label || address.address_line1} · {address.city}
                      </option>
                    ))}
                  </select>
                </div>
              ))}
              <div>
                <Label htmlFor="checkout-po">Your PO reference</Label>
                <Input
                  id="checkout-po"
                  name="customer_po_number"
                  maxLength={64}
                  className="mt-1 min-h-11"
                />
              </div>
              <div>
                <Label htmlFor="checkout-notes">Notes</Label>
                <Textarea id="checkout-notes" name="notes" maxLength={4000} className="mt-1" />
              </div>
              <FieldError
                message={
                  formError ||
                  (checkout.error
                    ? checkout.error instanceof ApiError && checkout.error.status === 409
                      ? 'Your cart changed. Close this form, refresh the cart, and review it before trying again.'
                      : dealerError(checkout.error)
                    : undefined)
                }
              />
            </DialogBody>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={closeCheckout}>
                Cancel
              </Button>
              <Button type="submit" loading={checkout.isPending} disabled={staleCart}>
                Place order
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  )
}

export function DealerWishlistPage() {
  const { can } = useAuth()
  const list = useDealerRead<SchemaOf<'DealerWishlistRead'>[]>('wishlist')
  const remove = useDealerAction(
    (id: string) => api.delete(`${DEALER_API}/wishlist/${id}`),
    'Removed from the wishlist',
  )
  const move = useDealerAction(
    (id: string) => api.post(`${DEALER_API}/wishlist/${id}/move-to-cart`),
    'Moved to the cart',
  )
  return (
    <>
      <PageHeader title="Wishlist" description="Products saved for your next order." />
      <FieldError
        message={
          move.error
            ? dealerError(move.error)
            : remove.error
              ? dealerError(remove.error)
              : undefined
        }
      />
      <DealerResult loading={list.isLoading} error={list.error}>
        {list.data?.length ? (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {list.data.map((item) => (
              <Card key={item.id}>
                <CardContent className="space-y-3 p-4">
                  <Link to={`/dealer/catalog/${item.item_id}`} className="font-medium underline">
                    {item.item_name}
                  </Link>
                  <p className="text-xs text-muted-foreground">{item.item_code}</p>
                  <p>{item.price_on_request ? 'Price on request' : formatMoney(item.price)}</p>
                  <StatusBadge status={item.availability_band} />
                  <div className="flex flex-wrap gap-2">
                    {can(P.DEALER_PORTAL_ORDER) && (
                      <Button
                        className="min-h-11"
                        onClick={() => move.mutate(item.id)}
                        disabled={move.isPending}
                      >
                        Move to cart
                      </Button>
                    )}
                    <Button
                      variant="outline"
                      className="min-h-11"
                      onClick={() => remove.mutate(item.id)}
                      disabled={remove.isPending}
                    >
                      Remove
                    </Button>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        ) : (
          <EmptyState
            title="Your wishlist is empty"
            description="Save products from the catalog to find them here."
          />
        )}
      </DealerResult>
    </>
  )
}

type Order = SchemaOf<'DealerOrderSummary'>
const orderColumns: ColumnDef<Order, unknown>[] = [
  {
    accessorKey: 'code',
    header: 'Order',
    cell: ({ row }) => (
      <Link to={`/dealer/orders/${row.original.id}`} className="font-medium underline">
        {row.original.code}
      </Link>
    ),
  },
  {
    accessorKey: 'status',
    header: 'Status',
    cell: ({ row }) => <StatusBadge status={row.original.status} />,
    enableSorting: false,
  },
  {
    accessorKey: 'order_date',
    header: 'Placed',
    cell: ({ row }) => formatDate(row.original.order_date),
  },
  {
    accessorKey: 'required_date',
    header: 'Required',
    cell: ({ row }) => formatDate(row.original.required_date),
  },
  {
    accessorKey: 'total_amount',
    header: 'Total',
    cell: ({ row }) => formatMoney(row.original.total_amount, { currency: row.original.currency }),
    enableSorting: false,
  },
]

export function DealerOrdersPage() {
  const list = useListQuery<Order>({
    path: `${DEALER_API}/orders`,
    queryKey: (params) => queryKeys.dealerPortal.list('orders', params),
    defaultSortBy: 'created_at',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
  })
  return (
    <>
      <PageHeader
        title="Orders"
        description="Orders placed by your account and their fulfilment progress."
      />
      <DataTable
        columns={orderColumns}
        data={list.data}
        pagination={list.pagination}
        isLoading={list.isLoading}
        isFetching={list.isFetching}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onSearchChange={list.setSearch}
        searchValue={list.search}
        onSortChange={list.setSort}
        onRefresh={() => void list.refetch()}
        emptyTitle="No orders yet"
      />
    </>
  )
}

function DealerTrackingPanel({ dispatchId }: { dispatchId: string }) {
  const tracking = useDealerRead<SchemaOf<'DealerTracking'>>(`dispatches/${dispatchId}/tracking`)
  return (
    <DealerResult loading={tracking.isLoading} error={tracking.error}>
      {tracking.data && (
        <ol className="mt-3 space-y-3 border-l pl-4">
          {tracking.data.events.map((event, index) => (
            <li key={`${event.event_time}-${index}`}>
              <StatusBadge status={event.event_status} />
              <p className="mt-1 text-sm">
                {formatDate(event.event_time)} · {event.location}
              </p>
              <p className="text-sm text-muted-foreground">{event.remark}</p>
            </li>
          ))}
          {!tracking.data.events.length && (
            <li className="text-sm text-muted-foreground">No tracking events yet.</li>
          )}
        </ol>
      )}
    </DealerResult>
  )
}

export function DealerOrderPage() {
  const { orderId = '' } = useParams()
  const detail = useDealerRead<SchemaOf<'DealerOrderDetail'>>(`orders/${orderId}`, Boolean(orderId))
  const dispatches = useDealerRead<SchemaOf<'DealerDispatchSummary'>[]>(
    `orders/${orderId}/dispatches`,
    Boolean(orderId),
  )
  const [cancelOpen, setCancelOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [reasonError, setReasonError] = useState<string>()
  const [trackingId, setTrackingId] = useState<string>()
  const cancel = useDealerAction(
    () => api.post(`${DEALER_API}/orders/${orderId}/cancel`, { reason: reason.trim() }),
    'Order cancelled',
    () => setCancelOpen(false),
  )
  const order = detail.data
  return (
    <>
      <PageHeader
        title={order?.code || 'Order'}
        breadcrumbs={[{ label: 'Orders', to: '/dealer/orders' }, { label: order?.code || 'Order' }]}
        status={<StatusBadge status={order?.status} />}
        actions={
          order?.status === 'DRAFT' && (
            <Button variant="destructive" onClick={() => setCancelOpen(true)}>
              Cancel order
            </Button>
          )
        }
      />
      <DealerResult loading={detail.isLoading} error={detail.error}>
        {order && (
          <div className="space-y-5">
            <DescriptionList
              items={[
                { label: 'Placed', value: formatDate(order.order_date) },
                { label: 'Required', value: formatDate(order.required_date) },
                { label: 'PO reference', value: order.customer_po_number || '—' },
                {
                  label: 'Total',
                  value: formatMoney(order.total_amount, { currency: order.currency }),
                },
              ]}
            />
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <caption className="sr-only">Order fulfilment by line</caption>
                <thead>
                  <tr>
                    {['Product', 'Ordered', 'Allocated', 'Dispatched', 'Invoiced', 'Total'].map(
                      (name) => (
                        <th key={name} className="p-2 text-left">
                          {name}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {order.lines.map((line) => (
                    <tr key={line.id} className="border-t">
                      <td className="p-2">
                        {line.item_name}
                        <p className="text-xs text-muted-foreground">{line.item_code}</p>
                      </td>
                      <td className="p-2">{formatQuantity(line.quantity)}</td>
                      <td className="p-2">{formatQuantity(line.allocated_quantity)}</td>
                      <td className="p-2">{formatQuantity(line.dispatched_quantity)}</td>
                      <td className="p-2">{formatQuantity(line.invoiced_quantity)}</td>
                      <td className="p-2">
                        {formatMoney(line.line_total, { currency: order.currency })}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <h2 className="font-semibold">Dispatches and tracking</h2>
            <DealerResult loading={dispatches.isLoading} error={dispatches.error}>
              <div className="space-y-3">
                {dispatches.data?.map((dispatch) => (
                  <Card key={dispatch.id}>
                    <CardContent className="space-y-3 p-4">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <h3 className="font-medium">{dispatch.code}</h3>
                        <StatusBadge status={dispatch.status} />
                      </div>
                      <DescriptionList
                        items={[
                          {
                            label: 'Carrier',
                            value: dispatch.courier_name || dispatch.transport_name || '—',
                          },
                          { label: 'Tracking ID', value: dispatch.tracking_id || '—' },
                          { label: 'LR number', value: dispatch.lr_number || '—' },
                          {
                            label: 'Expected delivery',
                            value: formatDate(dispatch.expected_delivery_date),
                          },
                        ]}
                      />
                      <Button
                        variant="outline"
                        className="min-h-11"
                        onClick={() =>
                          setTrackingId(trackingId === dispatch.id ? undefined : dispatch.id)
                        }
                        aria-expanded={trackingId === dispatch.id}
                      >
                        Tracking events
                      </Button>
                      {trackingId === dispatch.id && (
                        <DealerTrackingPanel dispatchId={dispatch.id} />
                      )}
                    </CardContent>
                  </Card>
                ))}
                {dispatches.data?.length === 0 && (
                  <p className="text-sm text-muted-foreground">
                    Dispatch details will appear when goods are ready to ship.
                  </p>
                )}
              </div>
            </DealerResult>
          </div>
        )}
      </DealerResult>
      <ConfirmDialog
        open={cancelOpen}
        onOpenChange={setCancelOpen}
        title="Cancel order?"
        description="The order will stay in your history."
        destructive
        confirmLabel="Cancel order"
        loading={cancel.isPending}
        onConfirm={() => {
          if (reason.trim().length < REASON_MIN_LENGTH)
            setReasonError(`Enter at least ${REASON_MIN_LENGTH} characters.`)
          else {
            setReasonError(undefined)
            cancel.mutate()
          }
        }}
      >
        <div className="space-y-2">
          <Label htmlFor="dealer-cancel-reason">Reason</Label>
          <Textarea
            id="dealer-cancel-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            maxLength={REASON_MAX_LENGTH}
          />
          <FieldError
            message={reasonError || (cancel.error ? dealerError(cancel.error) : undefined)}
          />
        </div>
      </ConfirmDialog>
    </>
  )
}

/**
 * Customer master list.
 *
 * Every filter is a query parameter the API understands: a dealer network runs
 * to thousands of accounts and the browser never receives more than one page
 * (master rules 31/36).
 *
 * The list projection carries no address, so the secondary line under the name
 * is the legal name rather than a city - the API stopped shipping a city on the
 * summary and addresses live behind their own paged endpoint.
 *
 * It carries `outstanding_amount`, though, and that is what lets the row offer
 * retirement honestly: the API refuses to retire an account that still owes
 * money, and the row already knows which accounts those are.
 */

import { type ColumnDef } from '@tanstack/react-table'
import { Infinity as InfinityIcon, Pencil, Plus, ShieldAlert, Trash2, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/overlays'
import { Badge } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { SELECT_ALL } from '@/lib/constants'
import { formatMoney, humanise, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'

import { CustomerFormDialog } from './customer-form-dialog'
import { RetireCustomerDialog } from './customer-retire-dialog'
import {
  acceptsRetirement,
  CUSTOMER_TYPES,
  type CustomerRow,
  hasOutstandingBalance,
  isDealerChannel,
} from './customer-types'

/** Which customer the dialog is open for: null = closed, id = edit, '' = create. */
type DialogTarget = string | null

export default function CustomersPage() {
  const { can } = useAuth()
  const [searchParams] = useSearchParams()
  const [dialogTarget, setDialogTarget] = useState<DialogTarget>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [retiring, setRetiring] = useState<CustomerRow | null>(null)

  const canCreate = can(P.CUSTOMER_CREATE)
  const canUpdate = can(P.CUSTOMER_UPDATE)
  // `DELETE /customers/{id}` declares `customer.delete`, which is what puts the
  // control on the row at all.
  const canDelete = can(P.CUSTOMER_DELETE)
  // The service then calls `assert_channel_write_allowed` on the way through and
  // demands `dealer.manage` as well before it will write a DEALER or
  // DISTRIBUTOR account. That right gates no route of its own, so it is checked
  // here per row rather than once for the screen: without it, the channel team's
  // accounts are the only ones this user may not retire.
  const canManageDealers = can(P.DEALER_MANAGE)

  const customerType = searchParams.get('customer_type') ?? ''
  const standing = searchParams.get('standing') ?? ''
  const activeState = searchParams.get('is_active') === 'false' ? 'false' : 'true'

  const list = useListQuery<CustomerRow>({
    path: '/customers',
    queryKey: queryKeys.customers.list,
    defaultSortBy: 'code',
    staleTime: staleTimes.master,
    filters: {
      ...(customerType ? { customer_type: customerType } : {}),
      ...(standing === 'blocked' ? { is_blocked: true } : {}),
      ...(standing === 'clear' ? { is_blocked: false } : {}),
      // The API decides who is over their limit - it holds the exposure the
      // browser cannot see, and a null limit means unlimited rather than zero.
      ...(standing === 'over_limit' ? { over_credit_limit: true } : {}),
      is_active: activeState !== 'false',
    },
  })

  const { setParam, setParams } = list
  const filterCount =
    (customerType ? 1 : 0) + (standing ? 1 : 0) + (activeState === 'false' ? 1 : 0)

  const openDialog = (target: DialogTarget) => {
    setDialogTarget(target)
    setDialogOpen(true)
  }

  const columns = useMemo<ColumnDef<CustomerRow, unknown>[]>(() => {
    const defs: ColumnDef<CustomerRow, unknown>[] = [
      {
        accessorKey: 'code',
        header: 'Code',
        cell: ({ row }) => <span className="font-medium">{row.original.code}</span>,
      },
      {
        accessorKey: 'name',
        header: 'Name',
        cell: ({ row }) => (
          <div className="max-w-[20rem]">
            <span className="block truncate">{row.original.name}</span>
            {row.original.legal_name && row.original.legal_name !== row.original.name && (
              <span className="block truncate text-xs text-muted-foreground">
                {row.original.legal_name}
              </span>
            )}
          </div>
        ),
      },
      {
        id: 'customer_type',
        header: 'Type',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="space-y-0.5">
            <Badge variant="secondary">{humanise(row.original.customer_type)}</Badge>
            {/* Which price list order entry will quote from. */}
            <span className="block text-xs text-muted-foreground">
              {humanise(row.original.price_list_type)} prices
            </span>
          </div>
        ),
      },
      {
        id: 'gstin',
        header: 'GSTIN',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.gstin ? (
            <span className="tabular text-xs">{row.original.gstin}</span>
          ) : (
            <span className="text-muted-foreground">Unregistered</span>
          ),
      },
      {
        accessorKey: 'credit_limit',
        header: 'Credit limit',
        meta: { align: 'right' },
        // Null is unlimited, not zero. Printing "₹0.00" for an account with no
        // ceiling would say the exact opposite of what the record means.
        cell: ({ row }) =>
          row.original.credit_limit === null ? (
            <span className="inline-flex items-center gap-1 text-muted-foreground">
              <InfinityIcon className="h-3.5 w-3.5" aria-hidden />
              Unlimited
            </span>
          ) : (
            formatMoney(row.original.credit_limit)
          ),
      },
      {
        accessorKey: 'outstanding_amount',
        header: 'Outstanding',
        meta: { align: 'right' },
        cell: ({ row }) => {
          // Compared as decimals: an account one rupee over its limit is a real
          // credit decision and must not ride on a float comparison.
          const limit = row.original.credit_limit
          const over =
            limit !== null &&
            toDecimal(limit).greaterThan(0) &&
            toDecimal(row.original.outstanding_amount).greaterThan(toDecimal(limit))
          return (
            <span className={over ? 'font-medium text-destructive' : undefined}>
              {formatMoney(row.original.outstanding_amount)}
            </span>
          )
        },
      },
      {
        accessorKey: 'credit_days',
        header: 'Credit days',
        // Not one of the API's sortable fields, and asking for it is a 400.
        enableSorting: false,
        meta: { align: 'right' },
        cell: ({ row }) => row.original.credit_days,
      },
      {
        id: 'is_blocked',
        header: 'Standing',
        enableSorting: false,
        // The reason lives on the customer record, not on the list projection,
        // so it is shown in the dialog rather than invented in a tooltip here.
        cell: ({ row }) =>
          row.original.is_blocked ? (
            <Badge variant="destructive">
              <ShieldAlert className="h-3 w-3" aria-hidden />
              Blocked
            </Badge>
          ) : (
            <span className="text-muted-foreground">Clear</span>
          ),
      },
      {
        id: 'is_active',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) => <StatusBadge status={row.original.is_active ? 'ACTIVE' : 'INACTIVE'} />,
      },
    ]

    // Permission gating here is UX only - the API authorises each call itself.
    if (canUpdate || canDelete) {
      defs.push({
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const customer = row.original

          // Retirement is offered only where the state machine has an edge for
          // it and the service would let this user take it. An archived account
          // never reaches this list - nothing here sends `include_deleted` - so
          // the state test is the machine's rule written down rather than a
          // condition that fires today; it is what stops a later "show retired
          // accounts" filter from quietly offering a second retirement.
          const mayRetire =
            canDelete &&
            acceptsRetirement(customer) &&
            (canManageDealers || !isDealerChannel(customer))

          // The service refuses outright while the account owes anything, so
          // the control is shown disabled with the amount rather than opening a
          // form whose only possible ending is a refusal.
          const owes = hasOutstandingBalance(customer)

          return (
            <div className="flex items-center justify-end gap-1">
              {canUpdate && (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Edit ${customer.code}`}
                  onClick={(event) => {
                    event.stopPropagation()
                    openDialog(customer.id)
                  }}
                >
                  <Pencil />
                </Button>
              )}

              {mayRetire &&
                (owes ? (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      {/* A disabled button emits no pointer events, so the
                          wrapper carries the trigger - on the button itself the
                          explanation would never appear, and a disabled control
                          is skipped by the tab order, so the wrapper takes the
                          focus instead. It also swallows the click: the row
                          behind it opens the edit dialog, which is not what
                          pressing a greyed-out Retire is asking for. */}
                      <span
                        className="inline-flex"
                        tabIndex={0}
                        onClick={(event) => event.stopPropagation()}
                      >
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          disabled
                          aria-label={`${customer.code} cannot be retired while ${formatMoney(
                            customer.outstanding_amount,
                          )} is outstanding`}
                        >
                          <Trash2 />
                        </Button>
                      </span>
                    </TooltipTrigger>
                    <TooltipContent className="max-w-xs">
                      {formatMoney(customer.outstanding_amount)} is still outstanding. An account
                      that owes money cannot be retired - settle or write off the balance first.
                    </TooltipContent>
                  </Tooltip>
                ) : (
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Retire ${customer.code}`}
                    onClick={(event) => {
                      event.stopPropagation()
                      setRetiring(customer)
                    }}
                  >
                    <Trash2 className="text-destructive" />
                  </Button>
                ))}
            </div>
          )
        },
      })
    }

    return defs
  }, [canUpdate, canDelete, canManageDealers])

  const newCustomerButton = canCreate ? (
    <Button onClick={() => openDialog('')}>
      <Plus />
      New customer
    </Button>
  ) : null

  return (
    <>
      <PageHeader
        title="Customers"
        description="Dealers, distributors and direct accounts, with the credit standing that governs order entry."
        actions={newCustomerButton}
      />

      <DataTable
        columns={columns}
        data={list.data}
        pagination={list.pagination}
        isLoading={list.isLoading}
        isFetching={list.isFetching}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onSortChange={list.setSort}
        onSearchChange={list.setSearch}
        onRefresh={() => void list.refetch()}
        searchValue={list.search}
        searchPlaceholder="Search code, name or GSTIN..."
        getRowId={(row) => row.id}
        onRowClick={canUpdate ? (row) => openDialog(row.id) : undefined}
        emptyTitle="No customers found"
        emptyDescription="Nothing matches the current filters. Widen the search or clear a filter."
        emptyAction={newCustomerButton}
        toolbar={
          <>
            <Select
              value={customerType || SELECT_ALL}
              onValueChange={(value) =>
                setParam('customer_type', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[170px]" aria-label="Filter by customer type">
                <SelectValue placeholder="All types" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>All types</SelectItem>
                {CUSTOMER_TYPES.map((type) => (
                  <SelectItem key={type} value={type}>
                    {humanise(type)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={standing || SELECT_ALL}
              onValueChange={(value) =>
                setParam('standing', value === SELECT_ALL ? undefined : value)
              }
            >
              <SelectTrigger className="w-full sm:w-[190px]" aria-label="Filter by credit standing">
                <SelectValue placeholder="Any standing" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SELECT_ALL}>Any standing</SelectItem>
                <SelectItem value="blocked">Blocked only</SelectItem>
                <SelectItem value="clear">Not blocked</SelectItem>
                <SelectItem value="over_limit">Over credit limit</SelectItem>
              </SelectContent>
            </Select>

            <Select value={activeState} onValueChange={(value) => setParam('is_active', value)}>
              <SelectTrigger className="w-full sm:w-[140px]" aria-label="Filter by status">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="true">Active only</SelectItem>
                <SelectItem value="false">Inactive only</SelectItem>
              </SelectContent>
            </Select>

            {filterCount > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  // One navigation, not one per filter: `setSearchParams` computes every
                  // functional update in a tick from the same captured value, so a loop of
                  // `setParam` calls clears exactly the last one and looks like it did nothing.
                  setParams({
                    customer_type: undefined,
                    standing: undefined,
                    is_active: undefined,
                  })
                }}
              >
                <X />
                Clear
              </Button>
            )}
          </>
        }
      />

      {(canCreate || canUpdate) && dialogTarget !== null && (
        <CustomerFormDialog
          open={dialogOpen}
          onOpenChange={(open) => {
            setDialogOpen(open)
            // Unmounted on close so the next opening starts from a clean form
            // rather than the previous customer's values.
            if (!open) setDialogTarget(null)
          }}
          customerId={dialogTarget === '' ? null : dialogTarget}
          canEdit={dialogTarget === '' ? canCreate : canUpdate}
          // The same channel rule as Retire above, applied where the loaded
          // record is known: the dialog withholds Save, Block and Deactivate on
          // a dealer or distributor account from a principal without it.
          canManageDealers={canManageDealers}
        />
      )}

      {/* Always rendered, unlike the form dialog above: it carries the reason
          box and the API's refusal, and clears both itself when it closes. */}
      <RetireCustomerDialog
        customer={retiring}
        open={retiring !== null}
        onOpenChange={(open) => !open && setRetiring(null)}
      />
    </>
  )
}

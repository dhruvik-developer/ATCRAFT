import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'

import { DataTable } from '@/components/data-table'
import { PageHeader } from '@/components/shared'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { entityLink } from '@/features/audit/audit-types'
import { useListQuery } from '@/hooks/use-list-query'
import { APPROVAL_PERMISSIONS } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { formatDateTime, humanise } from '@/lib/format'
import type { SchemaOf } from '@/types/api'

type ApprovalRequest = SchemaOf<'ApprovalRequestRead'>

export default function ApprovalsPage() {
  const { canAny } = useAuth()
  const approvals = useListQuery<ApprovalRequest>({
    path: '/audit/approvals',
    queryKey: queryKeys.audit.approvals,
    staleTime: staleTimes.dashboard,
  })

  const columns = useMemo<ColumnDef<ApprovalRequest, unknown>[]>(
    () => [
      {
        id: 'document',
        header: 'Document',
        enableSorting: false,
        cell: ({ row }) => {
          const request = row.original
          const href = entityLink(request.entity_type, request.entity_id)
          const label = request.entity_code ?? humanise(request.entity_type)
          return href ? (
            <Link to={href} className="font-medium text-primary underline-offset-4 hover:underline">
              {label}
            </Link>
          ) : (
            <span className="font-medium">{label}</span>
          )
        },
      },
      {
        accessorKey: 'requested_action',
        header: 'Approval required',
        enableSorting: false,
        cell: ({ row }) => humanise(row.original.requested_action.replaceAll('.', '_')),
      },
      {
        accessorKey: 'reason',
        header: 'Reason',
        enableSorting: false,
        cell: ({ row }) => row.original.reason ?? '—',
      },
      {
        accessorKey: 'created_at',
        header: 'Requested',
        enableSorting: false,
        cell: ({ row }) => formatDateTime(row.original.created_at),
      },
      {
        id: 'availability',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.can_decide ? (
            <span>Open on the document</span>
          ) : (
            <span title={row.original.blocked_reason ?? undefined}>
              Waiting for another approver
            </span>
          ),
      },
    ],
    [],
  )

  const mayView = canAny(...APPROVAL_PERMISSIONS)

  return (
    <>
      <PageHeader
        title="Approvals"
        description="Pending business decisions you are authorised to review. Open each document to take its decision through the owning workflow."
        breadcrumbs={[{ label: 'Administration' }, { label: 'Approvals' }]}
      />

      {!mayView ? (
        <Alert variant="destructive">
          <AlertTitle>Approval access required</AlertTitle>
          <AlertDescription>
            This page requires a permission ending in <code>.approve</code> or <code>_approve</code>
            .
          </AlertDescription>
        </Alert>
      ) : (
        <>
          {approvals.error && (
            <Alert variant="destructive">
              <AlertTitle>Could not load approvals</AlertTitle>
              <AlertDescription>{approvals.error.message}</AlertDescription>
            </Alert>
          )}
          <DataTable
            columns={columns}
            data={approvals.data}
            pagination={approvals.pagination}
            isLoading={approvals.isLoading}
            isFetching={approvals.isFetching}
            error={approvals.error}
            onRefresh={() => void approvals.refetch()}
            onPageChange={approvals.setPage}
            onLimitChange={approvals.setLimit}
            emptyTitle="Nothing waiting on you"
            emptyDescription="Approvals raised for your roles will appear here."
          />
        </>
      )}
    </>
  )
}

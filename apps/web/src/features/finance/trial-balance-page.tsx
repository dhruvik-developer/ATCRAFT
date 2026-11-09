/**
 * Trial balance.
 *
 * Every account's opening balance, what moved in the financial year to date and
 * where it closes, grouped by account class with a subtotal per class. The
 * report exists to prove one fact - the closing debit balances equal the closing
 * credit balances - so the screen states that fact rather than leaving the
 * reader to add two columns of decimals.
 *
 * The period is the whole financial year containing the chosen date, not a
 * calendar month: a balance sheet only balances when every posting since the
 * year opened is included. The API picks the active `financial_years` row (or
 * the company's configured start month when none has been opened) and returns
 * the period it used, which is what is printed here.
 *
 * `balanced` is the server's comparison of the two totals. A false value is not
 * a display quirk: the ledger itself does not balance, and the warning says so
 * in those words.
 *
 * The XLSX export is a job, not a file in the response. The button queues it and
 * the job strip below polls until the workbook is ready, exactly like every
 * other report export.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { FileSpreadsheet, Lock, Scale, TriangleAlert } from 'lucide-react'
import { useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'

import { EmptyState, LoadError, PageHeader, StatTile } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { ReportJobPanel } from '@/features/reports/job-panel'
import { useReportJob, type AcceptedJobData } from '@/features/reports/report-jobs'
import { api } from '@/lib/api-client'
import { formatDate, formatMoney, humanise, toDateInput, toDecimal } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'

import { ledgerKeys, type TrialBalance, type TrialBalanceGroup } from './ledger-model'

const MONEY_COLUMNS = [
  { key: 'opening_balance', label: 'Opening' },
  { key: 'period_debit', label: 'Period debit' },
  { key: 'period_credit', label: 'Period credit' },
  { key: 'closing_balance', label: 'Closing' },
] as const

function GroupTable({ group }: { group: TrialBalanceGroup }) {
  // `rows` is a defaulted list on the server, so the generated contract marks
  // it optional. An absent list means an account class with no accounts.
  const rows = group.rows ?? []

  return (
    <section
      className="rounded-lg border bg-card"
      aria-label={`${humanise(group.account_type)} accounts`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b px-4 py-2.5">
        <h2 className="text-sm font-semibold">{humanise(group.account_type)}</h2>
        <span className="text-xs text-muted-foreground">
          {rows.length} {rows.length === 1 ? 'account' : 'accounts'}
        </span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="px-4 py-2 font-medium">
                Code
              </th>
              <th scope="col" className="px-4 py-2 font-medium">
                Account
              </th>
              {MONEY_COLUMNS.map((column) => (
                <th key={column.key} scope="col" className="px-4 py-2 text-right font-medium">
                  {column.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.account_id} className="border-b last:border-0">
                <td className="whitespace-nowrap px-4 py-1.5 font-mono text-xs">
                  {row.account_code}
                </td>
                <td className="max-w-[22rem] truncate px-4 py-1.5">{row.account_name}</td>
                {MONEY_COLUMNS.map((column) => (
                  <td
                    key={column.key}
                    className="whitespace-nowrap px-4 py-1.5 text-right tabular-nums"
                  >
                    {formatMoney(row[column.key])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="border-t bg-muted/40 font-medium">
              <td className="px-4 py-2" colSpan={2}>
                {humanise(group.account_type)} subtotal
              </td>
              {MONEY_COLUMNS.map((column) => (
                <td
                  key={column.key}
                  className="whitespace-nowrap px-4 py-2 text-right tabular-nums"
                >
                  {formatMoney(group[column.key])}
                </td>
              ))}
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  )
}

function TrialBalanceStatement({ report }: { report: TrialBalance }) {
  const groups = report.groups ?? []
  const accounts = groups.reduce((total, group) => total + (group.rows?.length ?? 0), 0)

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Accounts"
          value={String(accounts)}
          hint={`Period ${formatDate(report.period_start)} to ${formatDate(report.period_end)}`}
        />
        <StatTile
          label="Total period debit"
          value={formatMoney(report.total_period_debit)}
          hint="Movement in the financial year to date"
        />
        <StatTile
          label="Total period credit"
          value={formatMoney(report.total_period_credit)}
          hint="Movement in the financial year to date"
        />
        <StatTile
          label="Closing debit / credit"
          value={`${formatMoney(report.total_debit_balance)} / ${formatMoney(report.total_credit_balance)}`}
          hint="The two sides must agree"
          tone={report.balanced ? 'default' : 'destructive'}
        />
      </div>

      {!report.balanced && (
        <Alert variant="destructive">
          <AlertTitle className="flex items-center gap-2">
            <TriangleAlert className="h-4 w-4" aria-hidden />
            The trial balance does not balance
          </AlertTitle>
          <AlertDescription>
            The closing debit balances and the closing credit balances differ by{' '}
            {formatMoney(
              toDecimal(report.total_debit_balance)
                .minus(toDecimal(report.total_credit_balance))
                .toFixed(2),
            )}
            . The ledger itself is inconsistent; a controller should reconcile the postings rather
            than use these figures.
          </AlertDescription>
        </Alert>
      )}

      {groups.map((group) => (
        <GroupTable key={group.account_type} group={group} />
      ))}

      <div className="rounded-lg border bg-card px-4 py-3">
        <div className="grid gap-x-8 gap-y-1 text-sm sm:grid-cols-2 lg:grid-cols-3">
          <div className="flex justify-between gap-3">
            <span className="text-muted-foreground">Total opening</span>
            <span className="tabular-nums">{formatMoney(report.total_opening_balance)}</span>
          </div>
          <div className="flex justify-between gap-3">
            <span className="text-muted-foreground">Total period debit</span>
            <span className="tabular-nums">{formatMoney(report.total_period_debit)}</span>
          </div>
          <div className="flex justify-between gap-3">
            <span className="text-muted-foreground">Total period credit</span>
            <span className="tabular-nums">{formatMoney(report.total_period_credit)}</span>
          </div>
          <div className="flex justify-between gap-3">
            <span className="text-muted-foreground">Closing debit balances</span>
            <span className="tabular-nums">{formatMoney(report.total_debit_balance)}</span>
          </div>
          <div className="flex justify-between gap-3">
            <span className="text-muted-foreground">Closing credit balances</span>
            <span className="tabular-nums">{formatMoney(report.total_credit_balance)}</span>
          </div>
          <div className="flex justify-between gap-3 font-medium">
            <span>Balanced</span>
            <span>{report.balanced ? 'Yes' : 'No'}</span>
          </div>
        </div>
      </div>
    </div>
  )
}

export default function TrialBalancePage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const canViewLedger = can(P.LEDGER_VIEW)
  const canExport = can(P.REPORT_EXPORT)

  const asOf = searchParams.get('as_of') || toDateInput(new Date())
  const params = useMemo(() => ({ as_of: asOf }), [asOf])

  const report = useQuery({
    queryKey: ledgerKeys.trialBalance(params),
    queryFn: ({ signal }) => api.get<TrialBalance>('/finance/trial-balance', { params, signal }),
    staleTime: staleTimes.transactional,
    enabled: canViewLedger,
  })

  const job = useReportJob()
  const queueExport = useMutation({
    mutationFn: () =>
      api.post<AcceptedJobData>('/finance/trial-balance/export', undefined, { params }),
    // An export reads; nothing is invalidated.
    onSuccess: (accepted) => job.start(accepted),
  })

  const header = (
    <PageHeader
      title="Trial balance"
      description="Every account's opening balance, movement in the financial year to date and closing balance, grouped by account class. The closing debit and credit balances must agree."
      breadcrumbs={[{ label: 'Finance', to: '/finance' }, { label: 'Trial balance' }]}
      actions={
        canViewLedger &&
        canExport && (
          <Button
            variant="outline"
            onClick={() => queueExport.mutate()}
            loading={queueExport.isPending}
          >
            <FileSpreadsheet />
            Export XLSX
          </Button>
        )
      }
    />
  )

  if (!canViewLedger) {
    return (
      <div>
        {header}
        <EmptyState
          icon={Lock}
          title="The ledger is not visible to this account"
          description="Reading the trial balance needs the ledger view right (ledger.view). An administrator can grant it against your role."
        />
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {header}

      <div className="flex flex-wrap items-end gap-3 rounded-lg border bg-card px-4 py-3">
        <div className="space-y-1.5">
          <Label htmlFor="trial-balance-as-of">As at</Label>
          <Input
            id="trial-balance-as-of"
            type="date"
            value={asOf}
            className="w-auto"
            onChange={(event) => {
              const next = new URLSearchParams(searchParams)
              if (event.target.value) next.set('as_of', event.target.value)
              else next.delete('as_of')
              setSearchParams(next, { replace: true })
            }}
          />
        </div>
        <p className="max-w-2xl text-xs text-muted-foreground">
          The period is the financial year containing this date. A trial balance cannot include
          postings that have not happened yet, so the period ends here.
        </p>
      </div>

      <ReportJobPanel controller={job} label="Trial balance" />

      {report.isLoading ? (
        <div className="space-y-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-56 w-full" />
        </div>
      ) : report.error ? (
        <LoadError
          error={report.error}
          title="Could not load the trial balance"
          onRetry={() => void report.refetch()}
          retrying={report.isFetching}
        />
      ) : report.data ? (
        <TrialBalanceStatement report={report.data} />
      ) : (
        <EmptyState
          icon={Scale}
          title="No accounts to report"
          description="The chart of accounts is empty, so there is nothing to balance."
        />
      )}
    </div>
  )
}

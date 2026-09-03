/**
 * Finance - ledgers, outstanding, ageing, valuation and GST in one place.
 *
 * The tab lives in the URL so a controller can send a colleague straight to the
 * ageing of payables rather than "open Finance, then click the third tab".
 *
 * Switching tabs clears the other query parameters. Each tab drives its own
 * paging, sorting and filters through the same URL keys, and carrying page 4 of
 * the outstanding list into the ageing table would silently show the wrong
 * slice of a different dataset.
 *
 * Two tabs are permission-gated. That is presentation: the API refuses the
 * request regardless of which triggers this file renders (master rules 38).
 */

import { format } from 'date-fns'
import {
  CalendarClock,
  Coins,
  FileText,
  Layers,
  Receipt,
  Scale,
  type LucideIcon,
} from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'

import { PageHeader } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/overlays'
import { useAuth } from '@/features/auth/auth-context'
import { P } from '@/lib/permissions'

import { AgeingTab } from './ageing-tab'
import { GstSummaryTab } from './gst-summary-tab'
import { InventoryValuationTab } from './inventory-valuation-tab'
import { OutstandingTab } from './outstanding-tab'
import { PartyLedgerTab } from './party-ledger-tab'

type TabKey = 'outstanding' | 'ageing' | 'ledger' | 'valuation' | 'gst'

export default function FinancePage() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const canSeeValuation = can(P.INVENTORY_VALUATION_VIEW)
  const canSeeGst = can(P.GST_REPORT_VIEW)

  const tabs: { key: TabKey; label: string; icon: LucideIcon }[] = [
    { key: 'outstanding', label: 'Outstanding', icon: Coins },
    { key: 'ageing', label: 'Ageing', icon: CalendarClock },
    { key: 'ledger', label: 'Ledger', icon: Scale },
    ...(canSeeValuation
      ? [{ key: 'valuation' as const, label: 'Inventory valuation', icon: Layers }]
      : []),
    ...(canSeeGst ? [{ key: 'gst' as const, label: 'GST summary', icon: FileText }] : []),
  ]

  const requested = searchParams.get('tab') as TabKey | null
  // A bookmarked link to a tab this account cannot open falls back rather than
  // rendering an empty shell.
  const tab: TabKey = tabs.some((entry) => entry.key === requested)
    ? (requested as TabKey)
    : 'outstanding'

  const period = searchParams.get('period') ?? format(new Date(), 'yyyy-MM')

  const selectTab = (next: string) => {
    setSearchParams(new URLSearchParams({ tab: next }), { replace: true })
  }

  const selectPeriod = (next: string) => {
    setSearchParams(
      (current) => {
        const updated = new URLSearchParams(current)
        if (next) updated.set('period', next)
        else updated.delete('period')
        return updated
      },
      { replace: true },
    )
  }

  return (
    <div>
      <PageHeader
        title="Finance"
        description="What is owed, how old it is, what the stock is worth and where GST stands this period."
        actions={
          can(P.VENDOR_INVOICE_VIEW) && (
            <Button variant="outline" asChild>
              <Link to="/finance/vendor-invoices">
                <Receipt />
                Vendor invoices
              </Link>
            </Button>
          )
        }
      />

      <Tabs value={tab} onValueChange={selectTab}>
        <TabsList className="h-auto flex-wrap">
          {tabs.map((entry) => (
            <TabsTrigger key={entry.key} value={entry.key}>
              <entry.icon className="h-4 w-4" aria-hidden />
              {entry.label}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="outstanding">
          <OutstandingTab />
        </TabsContent>

        <TabsContent value="ageing">
          <AgeingTab />
        </TabsContent>

        <TabsContent value="ledger">
          <PartyLedgerTab />
        </TabsContent>

        {canSeeValuation && (
          <TabsContent value="valuation">
            <InventoryValuationTab />
          </TabsContent>
        )}

        {canSeeGst && (
          <TabsContent value="gst">
            <GstSummaryTab period={period} onPeriodChange={selectPeriod} />
          </TabsContent>
        )}
      </Tabs>
    </div>
  )
}

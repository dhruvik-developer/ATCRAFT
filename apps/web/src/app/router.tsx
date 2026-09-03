/**
 * Route table.
 *
 * Feature pages are lazy-loaded: a store keeper who never opens Finance should
 * not download the finance bundle (master rules 36 - code splitting). The shell,
 * auth and dashboard are eager, since they are needed on first paint.
 */

import { Loader2 } from 'lucide-react'
import { Suspense, lazy, type ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { AppLayout } from '@/app/app-layout'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/features/auth/auth-context'
import { LoginPage } from '@/features/auth/login-page'
import { P, type PermissionKey } from '@/lib/permissions'

// --- lazy feature routes -----------------------------------------------------
const DashboardPage = lazy(() => import('@/features/dashboard/dashboard-page'))
const ItemsPage = lazy(() => import('@/features/items/items-page'))
const ItemDetailPage = lazy(() => import('@/features/items/item-detail-page'))
const WarehousesPage = lazy(() => import('@/features/warehouses/warehouses-page'))
const InventoryBalancesPage = lazy(() => import('@/features/inventory/balances-page'))
const InventoryLedgerPage = lazy(() => import('@/features/inventory/ledger-page'))
const ReservationsPage = lazy(() => import('@/features/inventory/reservations-page'))
const MovementsPage = lazy(() => import('@/features/inventory/movements-page'))
const BomListPage = lazy(() => import('@/features/bom/bom-list-page'))
const BomDetailPage = lazy(() => import('@/features/bom/bom-detail-page'))
const RoutingPage = lazy(() => import('@/features/routing/routing-page'))
const SalesOrdersPage = lazy(() => import('@/features/sales/sales-orders-page'))
const SalesOrderDetailPage = lazy(() => import('@/features/sales/sales-order-detail-page'))
const CustomersPage = lazy(() => import('@/features/customers/customers-page'))
const MrpPage = lazy(() => import('@/features/mrp/mrp-page'))
const MrpRunDetailPage = lazy(() => import('@/features/mrp/mrp-run-detail-page'))
const RequisitionsPage = lazy(() => import('@/features/procurement/requisitions-page'))
const PurchaseOrdersPage = lazy(() => import('@/features/procurement/purchase-orders-page'))
const PurchaseOrderDetailPage = lazy(
  () => import('@/features/procurement/purchase-order-detail-page'),
)
const GoodsReceiptsPage = lazy(() => import('@/features/procurement/goods-receipts-page'))
const VendorsPage = lazy(() => import('@/features/vendors/vendors-page'))
const ProductionPage = lazy(() => import('@/features/production/production-page'))
const ProductionDetailPage = lazy(() => import('@/features/production/production-detail-page'))
const AssemblyPage = lazy(() => import('@/features/assembly/assembly-page'))
const JobWorkPage = lazy(() => import('@/features/job-work/job-work-page'))
const QualityPage = lazy(() => import('@/features/quality/quality-page'))
const PackingPage = lazy(() => import('@/features/packing/packing-page'))
const DispatchPage = lazy(() => import('@/features/dispatch/dispatch-page'))
const FinancePage = lazy(() => import('@/features/finance/finance-page'))
const VendorInvoicesPage = lazy(() => import('@/features/finance/vendor-invoices-page'))
const ReportsPage = lazy(() => import('@/features/reports/reports-page'))
const AuditPage = lazy(() => import('@/features/audit/audit-page'))
const UsersPage = lazy(() => import('@/features/admin/users-page'))
const OrganisationPage = lazy(() => import('@/features/admin/organisation-page'))
const NotificationsPage = lazy(() => import('@/features/notifications/notifications-page'))
const PaymentsPage = lazy(() => import('@/features/finance/payments-page'))
const AccountsPage = lazy(() => import('@/features/finance/accounts-page'))
const RfqsPage = lazy(() => import('@/features/procurement/rfqs-page'))
const RfqDetailPage = lazy(() => import('@/features/procurement/rfq-detail-page'))
const QuotationsPage = lazy(() => import('@/features/procurement/quotations-page'))
const PurchaseReturnsPage = lazy(() => import('@/features/procurement/purchase-returns-page'))
const TransportersPage = lazy(() => import('@/features/transporters/transporters-page'))
const WorkOrdersPage = lazy(() => import('@/features/production/work-orders-page'))
const QcTemplatesPage = lazy(() => import('@/features/quality/qc-templates-page'))
const DocumentsPage = lazy(() => import('@/features/documents/documents-page'))
const MastersPage = lazy(() => import('@/features/masters/masters-page'))
const LocationsPage = lazy(() => import('@/features/warehouses/locations-page'))
const DealersPage = lazy(() => import('@/features/customers/dealers-page'))
const ProfilePage = lazy(() => import('@/features/auth/profile-page'))

function PageLoader() {
  return (
    <div className="grid h-64 place-items-center" role="status" aria-label="Loading">
      <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
    </div>
  )
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthenticated, isRestoring } = useAuth()
  const location = useLocation()

  // Restoring must not redirect: a hard reload has a refresh token but no user
  // yet, and bouncing to /login here would sign people out on every F5.
  if (isRestoring) {
    return (
      <div className="grid min-h-dvh place-items-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    )
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }

  return <>{children}</>
}

/**
 * Route-level permission gate.
 *
 * Shows an explicit "not permitted" page rather than redirecting: a user who
 * followed a colleague's link deserves to know why the page will not open.
 */
function RequirePermission({
  permissions,
  children,
}: {
  permissions: string[]
  children: ReactNode
}) {
  const { canAny } = useAuth()
  if (permissions.length > 0 && !canAny(...permissions)) {
    return (
      <div className="mx-auto max-w-md py-20 text-center">
        <h1 className="text-lg font-semibold">You do not have access to this page</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Your account does not hold the permission this screen requires. Ask an administrator if
          you believe this is wrong.
        </p>
        <Button variant="outline" className="mt-6" asChild>
          <a href="/">Back to dashboard</a>
        </Button>
      </div>
    )
  }
  return <>{children}</>
}

function NotFound() {
  return (
    <div className="mx-auto max-w-md py-20 text-center">
      <p className="text-4xl font-bold text-muted-foreground/40">404</p>
      <h1 className="mt-2 text-lg font-semibold">Page not found</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        The page you are looking for does not exist or has been moved.
      </p>
      <Button variant="outline" className="mt-6" asChild>
        <a href="/">Back to dashboard</a>
      </Button>
    </div>
  )
}

export function AppRouter() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route
        element={
          <RequireAuth>
            <AppLayout />
          </RequireAuth>
        }
      >
        <Route
          index
          element={
            <Suspense fallback={<PageLoader />}>
              <DashboardPage />
            </Suspense>
          }
        />

        {routeGroups.map(({ path, element, permissions }) => (
          <Route
            key={path}
            path={path}
            element={
              <RequirePermission permissions={permissions}>
                <Suspense fallback={<PageLoader />}>{element}</Suspense>
              </RequirePermission>
            }
          />
        ))}

        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}

/**
 * Declared as data so a route and its permission cannot drift apart, and so the
 * list can be asserted against the sidebar in a test.
 *
 * Permissions come from the `P` catalogue rather than raw strings, and the array
 * is typed `PermissionKey[]`: a mistyped permission is then a compile error
 * rather than a route that silently hides itself from everyone.
 */
const routeGroups: { path: string; element: ReactNode; permissions: PermissionKey[] }[] = [
  { path: '/items', element: <ItemsPage />, permissions: [P.ITEM_VIEW] },
  { path: '/items/:itemId', element: <ItemDetailPage />, permissions: [P.ITEM_VIEW] },
  { path: '/warehouses', element: <WarehousesPage />, permissions: [P.WAREHOUSE_VIEW] },

  { path: '/inventory', element: <InventoryBalancesPage />, permissions: [P.INVENTORY_VIEW] },
  {
    path: '/inventory/ledger',
    element: <InventoryLedgerPage />,
    permissions: [P.INVENTORY_LEDGER_VIEW],
  },
  {
    path: '/inventory/reservations',
    element: <ReservationsPage />,
    permissions: [P.RESERVATION_VIEW, P.ALLOCATION_VIEW],
  },
  {
    path: '/inventory/movements',
    element: <MovementsPage />,
    permissions: [P.INVENTORY_TRANSFER, P.INVENTORY_ADJUST],
  },

  { path: '/bom', element: <BomListPage />, permissions: [P.BOM_VIEW] },
  { path: '/bom/:bomId', element: <BomDetailPage />, permissions: [P.BOM_VIEW] },
  { path: '/routing', element: <RoutingPage />, permissions: [P.ROUTING_VIEW] },

  { path: '/sales-orders', element: <SalesOrdersPage />, permissions: [P.SALES_ORDER_VIEW] },
  {
    path: '/sales-orders/:orderId',
    element: <SalesOrderDetailPage />,
    permissions: [P.SALES_ORDER_VIEW],
  },
  { path: '/customers', element: <CustomersPage />, permissions: [P.CUSTOMER_VIEW] },

  { path: '/mrp', element: <MrpPage />, permissions: [P.MRP_VIEW] },
  { path: '/mrp/runs/:runId', element: <MrpRunDetailPage />, permissions: [P.MRP_VIEW] },

  {
    path: '/procurement/requisitions',
    element: <RequisitionsPage />,
    permissions: [P.PURCHASE_REQUISITION_VIEW],
  },
  {
    path: '/procurement/purchase-orders',
    element: <PurchaseOrdersPage />,
    permissions: [P.PURCHASE_ORDER_VIEW],
  },
  {
    path: '/procurement/purchase-orders/:poId',
    element: <PurchaseOrderDetailPage />,
    permissions: [P.PURCHASE_ORDER_VIEW],
  },
  {
    path: '/procurement/goods-receipts',
    element: <GoodsReceiptsPage />,
    permissions: [P.GRN_VIEW],
  },
  { path: '/vendors', element: <VendorsPage />, permissions: [P.VENDOR_VIEW] },

  { path: '/production', element: <ProductionPage />, permissions: [P.PRODUCTION_ORDER_VIEW] },
  {
    path: '/production/:orderId',
    element: <ProductionDetailPage />,
    permissions: [P.PRODUCTION_ORDER_VIEW],
  },
  { path: '/assembly', element: <AssemblyPage />, permissions: [P.ASSEMBLY_ORDER_VIEW] },
  { path: '/job-work', element: <JobWorkPage />, permissions: [P.JOB_WORK_VIEW] },

  { path: '/quality', element: <QualityPage />, permissions: [P.QC_VIEW] },
  { path: '/packing', element: <PackingPage />, permissions: [P.PACKING_VIEW] },
  { path: '/dispatch', element: <DispatchPage />, permissions: [P.DISPATCH_VIEW] },

  { path: '/finance', element: <FinancePage />, permissions: [P.FINANCE_VIEW] },
  {
    path: '/finance/vendor-invoices',
    element: <VendorInvoicesPage />,
    permissions: [P.VENDOR_INVOICE_VIEW],
  },

  // Each route is guarded by the read right its own endpoints declare, so the
  // screens below do not restate it at every call site. Write rights stay on
  // the individual affordance, because holding the read right says nothing
  // about whether the API will accept a change.
  { path: '/finance/payments', element: <PaymentsPage />, permissions: [P.PAYMENT_VIEW] },
  { path: '/finance/accounts', element: <AccountsPage />, permissions: [P.LEDGER_VIEW] },

  { path: '/procurement/rfqs', element: <RfqsPage />, permissions: [P.RFQ_VIEW] },
  { path: '/procurement/rfqs/:rfqId', element: <RfqDetailPage />, permissions: [P.RFQ_VIEW] },
  { path: '/procurement/quotations', element: <QuotationsPage />, permissions: [P.QUOTATION_VIEW] },
  {
    path: '/procurement/purchase-returns',
    element: <PurchaseReturnsPage />,
    permissions: [P.PURCHASE_RETURN_VIEW],
  },

  { path: '/transporters', element: <TransportersPage />, permissions: [P.TRANSPORTER_VIEW] },
  { path: '/dealers', element: <DealersPage />, permissions: [P.DEALER_VIEW] },
  { path: '/masters', element: <MastersPage />, permissions: [P.CATEGORY_VIEW, P.UOM_VIEW] },
  { path: '/warehouses/locations', element: <LocationsPage />, permissions: [P.LOCATION_VIEW] },

  { path: '/production/work-orders', element: <WorkOrdersPage />, permissions: [P.WORK_ORDER_VIEW] },
  { path: '/quality/templates', element: <QcTemplatesPage />, permissions: [P.QC_VIEW] },
  { path: '/documents', element: <DocumentsPage />, permissions: [P.DOCUMENT_VIEW] },

  { path: '/reports', element: <ReportsPage />, permissions: [P.REPORT_VIEW] },
  { path: '/audit', element: <AuditPage />, permissions: [P.AUDIT_LOG_VIEW] },

  { path: '/admin/users', element: <UsersPage />, permissions: [P.USER_VIEW, P.ROLE_VIEW] },
  { path: '/admin/organisation', element: <OrganisationPage />, permissions: [P.COMPANY_VIEW] },

  // Every notifications endpoint - list, unread count, mark-read, preferences -
  // is declared with require_permission(NOTIFICATION_VIEW), so an ungated route
  // let a user without it reach a page that could only 403. `/profile` below is
  // genuinely open: its endpoints are self-service `/auth/*` and gate on nothing.
  { path: '/notifications', element: <NotificationsPage />, permissions: [P.NOTIFICATION_VIEW] },
  { path: '/profile', element: <ProfilePage />, permissions: [] },
]

/**
 * Route table.
 *
 * Feature pages are lazy-loaded: a store keeper who never opens Finance should
 * not download the finance bundle (master rules 36 - code splitting). The shell
 * and the auth screens are eager, since they are needed on first paint; the
 * dashboard is lazy like every other page, and the login page warms its chunk
 * while the user is typing so the post-sign-in navigation is instant.
 */

import { Loader2 } from 'lucide-react'
import { Suspense, lazy, type ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { AppLayout } from '@/app/app-layout'
import { prefetchRoute, routeLoaders } from '@/app/route-loaders'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/features/auth/auth-context'
import { LoginPage } from '@/features/auth/login-page'
import { P, type PermissionKey } from '@/lib/permissions'

// --- lazy feature routes -----------------------------------------------------
// The import functions live in route-loaders.ts so the sidebar can warm a
// chunk on hover through the same promise `lazy` will resolve on click.
const DashboardPage = lazy(routeLoaders['/'])
const ItemsPage = lazy(routeLoaders['/items'])
const ItemDetailPage = lazy(routeLoaders['/items/:itemId'])
const WarehousesPage = lazy(routeLoaders['/warehouses'])
const InventoryBalancesPage = lazy(routeLoaders['/inventory'])
const InventoryLedgerPage = lazy(routeLoaders['/inventory/ledger'])
const ReservationsPage = lazy(routeLoaders['/inventory/reservations'])
const MovementsPage = lazy(routeLoaders['/inventory/movements'])
const BomListPage = lazy(routeLoaders['/bom'])
const BomDetailPage = lazy(routeLoaders['/bom/:bomId'])
const RoutingPage = lazy(routeLoaders['/routing'])
const SalesOrdersPage = lazy(routeLoaders['/sales-orders'])
const SalesOrderDetailPage = lazy(routeLoaders['/sales-orders/:orderId'])
const CustomersPage = lazy(routeLoaders['/customers'])
const MrpPage = lazy(routeLoaders['/mrp'])
const MrpRunDetailPage = lazy(routeLoaders['/mrp/runs/:runId'])
const RequisitionsPage = lazy(routeLoaders['/procurement/requisitions'])
const PurchaseOrdersPage = lazy(routeLoaders['/procurement/purchase-orders'])
const PurchaseOrderDetailPage = lazy(routeLoaders['/procurement/purchase-orders/:poId'])
const GoodsReceiptsPage = lazy(routeLoaders['/procurement/goods-receipts'])
const VendorsPage = lazy(routeLoaders['/vendors'])
const ProductionPage = lazy(routeLoaders['/production'])
const ProductionDetailPage = lazy(routeLoaders['/production/:orderId'])
const AssemblyPage = lazy(routeLoaders['/assembly'])
const JobWorkPage = lazy(routeLoaders['/job-work'])
const QualityPage = lazy(routeLoaders['/quality'])
const PackingPage = lazy(routeLoaders['/packing'])
const DispatchPage = lazy(routeLoaders['/dispatch'])
const FinancePage = lazy(routeLoaders['/finance'])
const VendorInvoicesPage = lazy(routeLoaders['/finance/vendor-invoices'])
const ReportsPage = lazy(routeLoaders['/reports'])
const AuditPage = lazy(routeLoaders['/audit'])
const UsersPage = lazy(routeLoaders['/admin/users'])
const OrganisationPage = lazy(routeLoaders['/admin/organisation'])
const NotificationsPage = lazy(routeLoaders['/notifications'])
const PaymentsPage = lazy(routeLoaders['/finance/payments'])
const AccountsPage = lazy(routeLoaders['/finance/accounts'])
const RfqsPage = lazy(routeLoaders['/procurement/rfqs'])
const RfqDetailPage = lazy(routeLoaders['/procurement/rfqs/:rfqId'])
const QuotationsPage = lazy(routeLoaders['/procurement/quotations'])
const PurchaseReturnsPage = lazy(routeLoaders['/procurement/purchase-returns'])
const TransportersPage = lazy(routeLoaders['/transporters'])
const ShiftRosterPage = lazy(routeLoaders['/production/shift-roster'])
const WorkOrdersPage = lazy(routeLoaders['/production/work-orders'])
const QcTemplatesPage = lazy(routeLoaders['/quality/templates'])
const DocumentsPage = lazy(routeLoaders['/documents'])
const MastersPage = lazy(routeLoaders['/masters'])
const LocationsPage = lazy(routeLoaders['/warehouses/locations'])
const DealersPage = lazy(routeLoaders['/dealers'])
const ProfilePage = lazy(routeLoaders['/profile'])
const ChangePasswordPage = lazy(routeLoaders['/change-password'])

// Lazy like every other page, and unlike the login screen beside them. These
// two are the only other routes an unauthenticated visitor can reach, and they
// are the reason react-hook-form, zod and @hookform/resolvers - 26 KB gzipped -
// sat in the eager entry chunk: the login form itself no longer uses them, so
// loading these on demand takes the whole form stack off the first paint.
const ForgotPasswordPage = lazy(() =>
  import('@/features/auth/password-reset-pages').then((m) => ({ default: m.ForgotPasswordPage })),
)
const ResetPasswordPage = lazy(() =>
  import('@/features/auth/password-reset-pages').then((m) => ({ default: m.ResetPasswordPage })),
)

function PageLoader() {
  return (
    <div className="grid h-64 place-items-center" role="status" aria-label="Loading">
      <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
    </div>
  )
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthenticated, isRestoring, user } = useAuth()
  const location = useLocation()

  // Restoring must not redirect: a hard reload has a refresh token but no user
  // yet, and bouncing to /login here would sign people out on every F5.
  if (isRestoring) {
    // A hard reload otherwise serialises three round trips: `/auth/me`, then the
    // route's chunk, then the page's own queries. The chunk depends on none of
    // them, so start it now and let it download alongside the auth call. Safe to
    // call during render - `prefetchRoute` only kicks off a module import, is
    // idempotent, and ignores a path it has no loader for (every detail route).
    prefetchRoute(location.pathname)
    return (
      <div className="grid min-h-dvh place-items-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    )
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }

  // A temporary password is a restricted authentication state, not a banner
  // the user may ignore. Keep it outside AppLayout so the shell cannot fire
  // unrelated requests that the API correctly refuses until it is replaced.
  if (user?.must_change_password && location.pathname !== '/change-password') {
    return <Navigate to="/change-password" replace />
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
        path="/forgot-password"
        element={
          <Suspense fallback={<PageLoader />}>
            <ForgotPasswordPage />
          </Suspense>
        }
      />
      <Route
        path="/reset-password"
        element={
          <Suspense fallback={<PageLoader />}>
            <ResetPasswordPage />
          </Suspense>
        }
      />
      <Route
        path="/change-password"
        element={
          <RequireAuth>
            <Suspense fallback={<PageLoader />}>
              <ChangePasswordPage />
            </Suspense>
          </RequireAuth>
        }
      />

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
  // `GET /allocation/reservations` declares RESERVATION_VIEW. `allocation.view`
  // only unlocks the availability panel inside the page, which gates it itself.
  {
    path: '/inventory/reservations',
    element: <ReservationsPage />,
    permissions: [P.RESERVATION_VIEW],
  },
  // Transfers, adjustments and physical counts are all listed under
  // INVENTORY_VIEW; each write right stays on the page's own button.
  { path: '/inventory/movements', element: <MovementsPage />, permissions: [P.INVENTORY_VIEW] },

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
  {
    path: '/production/shift-roster',
    element: <ShiftRosterPage />,
    permissions: [P.SHIFT_ROSTER_VIEW, P.SHIFT_VIEW],
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
  // Every tab's own read right opens the page, and each tab is gated on that
  // right inside it: a holder of only `brand.view` (or only `price.view`, whose
  // `GET /products/items/{id}/prices` declares PRICE_VIEW) must still get in.
  {
    path: '/masters',
    element: <MastersPage />,
    permissions: [
      P.CATEGORY_VIEW,
      P.UOM_VIEW,
      P.PRICE_VIEW,
      P.ITEM_ATTRIBUTE_VIEW,
      P.BRAND_VIEW,
      P.MANUFACTURER_VIEW,
    ],
  },
  { path: '/warehouses/locations', element: <LocationsPage />, permissions: [P.LOCATION_VIEW] },

  {
    path: '/production/work-orders',
    element: <WorkOrdersPage />,
    permissions: [P.WORK_ORDER_VIEW],
  },
  { path: '/quality/templates', element: <QcTemplatesPage />, permissions: [P.QC_VIEW] },
  { path: '/documents', element: <DocumentsPage />, permissions: [P.DOCUMENT_VIEW] },

  { path: '/reports', element: <ReportsPage />, permissions: [P.REPORT_VIEW] },
  { path: '/audit', element: <AuditPage />, permissions: [P.AUDIT_LOG_VIEW] },

  { path: '/admin/users', element: <UsersPage />, permissions: [P.USER_VIEW, P.ROLE_VIEW] },
  // `cost_centre.view` admits the accounts team, who keep the cost-centre
  // master, without handing them the company profile. Each tab is gated on its
  // own register's right inside the page.
  {
    path: '/admin/organisation',
    element: <OrganisationPage />,
    permissions: [P.COMPANY_VIEW, P.COST_CENTRE_VIEW],
  },

  // Every notifications endpoint - list, unread count, mark-read, preferences -
  // is declared with require_permission(NOTIFICATION_VIEW), so an ungated route
  // let a user without it reach a page that could only 403. `/profile` below is
  // genuinely open: its endpoints are self-service `/auth/*` and gate on nothing.
  { path: '/notifications', element: <NotificationsPage />, permissions: [P.NOTIFICATION_VIEW] },
  { path: '/profile', element: <ProfilePage />, permissions: [] },
]

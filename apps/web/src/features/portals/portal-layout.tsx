import { useQuery } from '@tanstack/react-query'
import {
  Bell,
  Home,
  LogOut,
  MoreHorizontal,
  Package,
  ShoppingBag,
  User,
  Wallet,
  type LucideIcon,
} from 'lucide-react'
import { NavLink, Outlet } from 'react-router-dom'

import { ThemeToggle } from '@/components/theme-toggle'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useAuth } from '@/features/auth/auth-context'
import { PushSessionBridge } from '@/features/notifications/push-session'
import { api } from '@/lib/api-client'
import { humanise } from '@/lib/format'
import { P, type PermissionKey } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { cn } from '@/lib/utils'
import type { SchemaOf } from '@/types/api'

interface PortalLink {
  to: string
  label: string
  permission?: PermissionKey
}

/** Portal accounts share session controls, but never mount the internal ERP shell. */
export default function PortalLayout() {
  const { user, isVendorUser, can, logout } = useAuth()
  const root = isVendorUser ? '/vendor' : '/dealer'
  const identity = useQuery<SchemaOf<'VendorPortalMe'> | SchemaOf<'DealerPortalMe'>>({
    queryKey: isVendorUser ? queryKeys.vendorPortal.me : queryKeys.dealerPortal.me,
    queryFn: ({ signal }) =>
      isVendorUser
        ? api.get<SchemaOf<'VendorPortalMe'>>('/vendor-portal/me', { signal })
        : api.get<SchemaOf<'DealerPortalMe'>>('/dealer-portal/me', { signal }),
    enabled: can(isVendorUser ? P.VENDOR_PORTAL_ACCESS : P.DEALER_PORTAL_BROWSE),
  })
  const accountName = identity.data
    ? 'name' in identity.data
      ? identity.data.name
      : identity.data.customer_name
    : user?.full_name
  const items: (PortalLink & { icon: LucideIcon })[] = isVendorUser
    ? [
        { to: root, label: 'Home', icon: Home, permission: P.VENDOR_PORTAL_ACCESS },
        {
          to: `${root}/orders`,
          label: 'Orders',
          icon: ShoppingBag,
          permission: P.VENDOR_PORTAL_ACCESS,
        },
        {
          to: `${root}/shipments`,
          label: 'Shipments',
          icon: Package,
          permission: P.VENDOR_PORTAL_ACCESS,
        },
        {
          to: `${root}/invoices`,
          label: 'Invoices',
          icon: Wallet,
          permission: P.VENDOR_PORTAL_INVOICE,
        },
        { to: '/profile', label: 'Account', icon: User },
      ]
    : [
        { to: root, label: 'Home', icon: Home, permission: P.DEALER_PORTAL_BROWSE },
        {
          to: `${root}/catalog`,
          label: 'Catalog',
          icon: Package,
          permission: P.DEALER_PORTAL_BROWSE,
        },
        { to: `${root}/cart`, label: 'Cart', icon: ShoppingBag, permission: P.DEALER_PORTAL_ORDER },
        {
          to: `${root}/orders`,
          label: 'Orders',
          icon: ShoppingBag,
          permission: P.DEALER_PORTAL_ORDER,
        },
        {
          to: `${root}/account`,
          label: 'Account',
          icon: User,
          permission: P.DEALER_PORTAL_ACCOUNT,
        },
      ]
  const more: (PortalLink & { permission: PermissionKey })[] = isVendorUser
    ? [
        { to: `${root}/receipts`, label: 'Receipts and QC', permission: P.VENDOR_PORTAL_ACCESS },
        { to: `${root}/payments`, label: 'Payments', permission: P.VENDOR_PORTAL_INVOICE },
      ]
    : [
        { to: `${root}/wishlist`, label: 'Wishlist', permission: P.DEALER_PORTAL_BROWSE },
        { to: `${root}/invoices`, label: 'Invoices', permission: P.DEALER_PORTAL_ACCOUNT },
        { to: `${root}/returns`, label: 'Returns', permission: P.DEALER_PORTAL_RETURN },
      ]

  return (
    <div className="min-h-dvh bg-background text-foreground">
      <PushSessionBridge />
      <a
        href="#portal-content"
        className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:bg-background focus:p-3"
      >
        Skip to content
      </a>
      <header className="sticky top-0 z-30 border-b bg-background">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4 py-3">
          <div className="min-w-0">
            <p className="truncate font-semibold">{user?.company_name || 'Atcraft ERP'}</p>
            <p className="truncate text-xs text-muted-foreground">
              {accountName} · {isVendorUser ? 'Vendor' : 'Dealer'} portal
            </p>
            {user?.roles.length ? (
              <p
                className="truncate text-xs text-muted-foreground"
                title={user.roles.map(humanise).join(', ')}
              >
                {user.roles.map(humanise).join(', ')}
              </p>
            ) : null}
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {can(P.NOTIFICATION_VIEW) && (
              <Button variant="ghost" size="icon" className="h-11 w-11" asChild>
                <NavLink to="/notifications" aria-label="Notifications">
                  <Bell className="h-5 w-5" />
                </NavLink>
              </Button>
            )}
            <ThemeToggle />
            <Button
              variant="ghost"
              size="icon"
              className="h-11 w-11"
              aria-label="Sign out"
              onClick={() => void logout()}
            >
              <LogOut className="h-5 w-5" />
            </Button>
          </div>
        </div>
      </header>
      <div className="mx-auto flex max-w-7xl">
        <aside className="hidden w-52 shrink-0 border-r p-4 md:block">
          <nav aria-label="Portal navigation" className="sticky top-24 space-y-1">
            {([...items, ...more, { to: '/profile', label: 'Profile' }] as PortalLink[])
              .filter(
                (item, index, all) => all.findIndex((other) => other.to === item.to) === index,
              )
              .filter((item) => !item.permission || can(item.permission))
              .map((item) => (
                <NavLink
                  key={item.to}
                  end={item.to === root}
                  to={item.to}
                  className={({ isActive }) =>
                    cn(
                      'flex min-h-11 items-center rounded-md px-3 text-sm',
                      isActive ? 'bg-primary text-primary-foreground' : 'hover:bg-muted',
                    )
                  }
                >
                  {item.label}
                </NavLink>
              ))}
          </nav>
        </aside>
        <main id="portal-content" className="min-w-0 flex-1 px-4 py-5 pb-28 md:px-6 md:pb-8">
          <Outlet />
          <nav
            aria-label="More portal pages"
            className="mt-8 flex flex-wrap gap-2 border-t pt-4 md:hidden"
          >
            {more
              .filter((item) => can(item.permission))
              .map((item) => (
                <Button key={item.to} variant="outline" className="min-h-11" asChild>
                  <NavLink to={item.to}>{item.label}</NavLink>
                </Button>
              ))}
            <Button variant="outline" className="min-h-11" asChild>
              <NavLink to="/profile">Profile</NavLink>
            </Button>
          </nav>
        </main>
      </div>
      <nav
        aria-label="Mobile portal navigation"
        className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t bg-background pb-[env(safe-area-inset-bottom)] md:hidden"
      >
        {items
          .filter((item) => !item.permission || can(item.permission))
          .map(({ to, label, icon: Icon }) =>
            isVendorUser && to === '/profile' ? (
              <DropdownMenu key="more">
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    className="h-auto min-h-16 flex-col gap-1 rounded-none px-1 text-xs"
                  >
                    <MoreHorizontal className="h-5 w-5" aria-hidden />
                    More
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent
                  side="top"
                  align="end"
                  className="max-h-[70dvh] w-60 overflow-y-auto"
                >
                  {more
                    .filter((item) => can(item.permission))
                    .map((item) => (
                      <DropdownMenuItem key={item.to} asChild>
                        <NavLink className="min-h-11" to={item.to}>
                          {item.label}
                        </NavLink>
                      </DropdownMenuItem>
                    ))}
                  {can(P.NOTIFICATION_VIEW) && (
                    <DropdownMenuItem asChild>
                      <NavLink className="min-h-11" to="/notifications">
                        Notifications
                      </NavLink>
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuItem asChild>
                    <NavLink className="min-h-11" to="/profile">
                      Profile
                    </NavLink>
                  </DropdownMenuItem>
                  <DropdownMenuItem className="min-h-11" onSelect={() => void logout()}>
                    Sign out
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            ) : (
              <NavLink
                key={to}
                end={to === root}
                to={to}
                className={({ isActive }) =>
                  cn(
                    'flex min-h-16 min-w-0 flex-col items-center justify-center gap-1 px-1 text-xs',
                    isActive ? 'text-primary' : 'text-muted-foreground',
                  )
                }
              >
                <Icon className="h-5 w-5" aria-hidden />
                <span className="truncate">{label}</span>
              </NavLink>
            ),
          )}
      </nav>
    </div>
  )
}

import { useQuery } from '@tanstack/react-query'
import {
  Bell,
  ChevronDown,
  LogOut,
  Menu,
  Moon,
  PanelLeftClose,
  Search,
  Sun,
  User as UserIcon,
  X,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'

import { navigation, type NavItem } from '@/app/navigation'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  TooltipProvider,
} from '@/components/ui/overlays'
import { Badge, Separator } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { P } from '@/lib/permissions'
import { api } from '@/lib/api-client'
import { queryKeys } from '@/lib/query-client'
import { cn, initials } from '@/lib/utils'
import type { SchemaOf } from '@/types/api'

const THEME_KEY = 'atcraft.theme'

function useTheme() {
  const [theme, setTheme] = useState<'light' | 'dark'>(
    () =>
      (localStorage.getItem(THEME_KEY) as 'light' | 'dark' | null) ??
      (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'),
  )

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    localStorage.setItem(THEME_KEY, theme)
  }, [theme])

  return { theme, toggle: () => setTheme((t) => (t === 'dark' ? 'light' : 'dark')) }
}

export function AppLayout() {
  const { user, logout, can, canAny } = useAuth()
  const { theme, toggle } = useTheme()
  const location = useLocation()
  const [mobileOpen, setMobileOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem('atcraft.sidebar') === 'collapsed',
  )

  // Close the mobile drawer on navigation, otherwise it covers the page the
  // user just asked for.
  useEffect(() => setMobileOpen(false), [location.pathname])

  useEffect(() => {
    localStorage.setItem('atcraft.sidebar', collapsed ? 'collapsed' : 'expanded')
  }, [collapsed])

  /**
   * The unread badge.
   *
   * The payload is `UnreadCountRead` - `unread_count`, plus `is_capped` when the
   * API stopped counting at its ceiling rather than walking an unbounded
   * backlog. It is read straight from the contract: this query used to ask for a
   * `count` field that does not exist, so `.then((r) => r.count)` resolved to
   * `undefined`, React Query rejected the result as an unusable value, and the
   * rejection was not an `ApiError` - so the global handler ignored it too. The
   * badge was permanently blank and nothing anywhere said why.
   *
   * The whole payload is returned rather than one field off it, so a failure is
   * a failure rather than a silent `undefined`, and the error is shown on the
   * bell instead of being swallowed.
   */
  // Gated, and it has to be: `GET /notifications/unread-count` declares
  // `require_permission(NOTIFICATION_VIEW)`, while this bell lives in the shell
  // and polls on every route. Ungated, a user without that right collected a 403
  // on every page they opened - once on load and again every minute - which is
  // invisible to an administrator, who holds every permission by construction.
  const maySeeNotifications = can(P.NOTIFICATION_VIEW)
  const unread = useQuery({
    queryKey: queryKeys.notifications.unreadCount,
    queryFn: () => api.get<SchemaOf<'UnreadCountRead'>>('/notifications/unread-count'),
    enabled: maySeeNotifications,
    refetchInterval: 60_000,
    staleTime: 30_000,
  })

  const unreadCount = unread.data?.unread_count ?? 0
  const unreadUnavailable = Boolean(unread.error)
  // A capped count is a floor, not a figure: "500+" is the honest reading.
  const unreadLabel = unread.data?.is_capped ? `${unreadCount}+` : String(unreadCount)

  const visibleSections = navigation
    .map((section) => ({
      ...section,
      items: section.items.filter(
        (item) => !item.permissions?.length || canAny(...item.permissions),
      ),
    }))
    .filter((section) => section.items.length > 0)

  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex h-full">
        {/* ------------------------------------------------------ sidebar -- */}
        <aside
          className={cn(
            'fixed inset-y-0 left-0 z-40 flex flex-col bg-sidebar text-sidebar-foreground transition-all duration-200',
            'lg:static lg:translate-x-0',
            collapsed ? 'w-[68px]' : 'w-64',
            mobileOpen ? 'translate-x-0' : '-translate-x-full',
          )}
        >
          <div className="flex h-14 items-center gap-2.5 border-b border-sidebar-border px-4">
            <div className="grid h-8 w-8 shrink-0 place-items-center rounded-md bg-sidebar-accent font-bold text-white">
              A
            </div>
            {!collapsed && (
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-semibold text-white">Atcraft ERP</p>
                <p className="truncate text-2xs text-sidebar-foreground/60">
                  {user?.company_name ?? ''}
                </p>
              </div>
            )}
            <button
              type="button"
              className="ml-auto rounded p-1 text-sidebar-foreground/70 hover:text-white lg:hidden"
              onClick={() => setMobileOpen(false)}
              aria-label="Close navigation"
            >
              <X className="h-5 w-5" />
            </button>
          </div>

          <nav className="flex-1 space-y-4 overflow-y-auto px-2.5 py-4" aria-label="Main">
            {visibleSections.map((section) => (
              <div key={section.label}>
                {!collapsed && (
                  <p className="px-2.5 pb-1.5 text-2xs font-semibold uppercase tracking-wider text-sidebar-foreground/70">
                    {section.label}
                  </p>
                )}
                <ul className="space-y-0.5">
                  {section.items.map((item) => (
                    <SidebarLink key={item.to} item={item} collapsed={collapsed} />
                  ))}
                </ul>
              </div>
            ))}
          </nav>

          <div className="border-t border-sidebar-border p-2.5">
            <button
              type="button"
              onClick={() => setCollapsed((c) => !c)}
              className="hidden w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-sidebar-foreground/70 hover:bg-white/5 hover:text-white lg:flex"
            >
              <PanelLeftClose
                className={cn('h-4 w-4 transition-transform', collapsed && 'rotate-180')}
              />
              {!collapsed && <span>Collapse</span>}
            </button>
          </div>
        </aside>

        {mobileOpen && (
          <div
            className="fixed inset-0 z-30 bg-black/50 lg:hidden"
            onClick={() => setMobileOpen(false)}
            aria-hidden
          />
        )}

        {/* ---------------------------------------------------- main area -- */}
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-20 flex h-14 shrink-0 items-center gap-3 border-b bg-background/95 px-4 backdrop-blur">
            <button
              type="button"
              className="rounded-md p-1.5 hover:bg-accent lg:hidden"
              onClick={() => setMobileOpen(true)}
              aria-label="Open navigation"
            >
              <Menu className="h-5 w-5" />
            </button>

            <div className="relative hidden max-w-md flex-1 md:block">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                type="search"
                placeholder="Search items, orders, vendors..."
                className="h-9 w-full rounded-md border border-input bg-background pl-8 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>

            <div className="ml-auto flex items-center gap-1">
              <Button variant="ghost" size="icon-sm" onClick={toggle} aria-label="Toggle theme">
                {theme === 'dark' ? <Sun /> : <Moon />}
              </Button>

              {/* Hidden outright without the right, rather than shown as a
                  permanently empty bell linking to a route the guard refuses. */}
              {maySeeNotifications && (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  asChild
                  aria-label={
                    unreadUnavailable
                      ? 'Notifications - the unread count could not be loaded'
                      : unreadCount > 0
                        ? `Notifications, ${unreadLabel} unread`
                        : 'Notifications'
                  }
                >
                  <Link
                    to="/notifications"
                    className="relative"
                    title={
                      unreadUnavailable
                        ? `The unread count could not be loaded: ${(unread.error as Error).message}`
                        : undefined
                    }
                  >
                    <Bell />
                    {/* A failing endpoint is marked, not hidden behind an empty
                      bell that reads as "nothing new". */}
                    {unreadUnavailable ? (
                      <span
                        className="absolute -right-0.5 -top-0.5 grid h-4 min-w-4 place-items-center rounded-full bg-warning px-1 text-[10px] font-semibold text-warning-foreground"
                        aria-hidden
                      >
                        !
                      </span>
                    ) : (
                      unreadCount > 0 && (
                        <span className="absolute -right-0.5 -top-0.5 grid h-4 min-w-4 place-items-center rounded-full bg-destructive px-1 text-[10px] font-semibold text-destructive-foreground">
                          {unreadCount > 99 ? '99+' : unreadLabel}
                        </span>
                      )
                    )}
                  </Link>
                </Button>
              )}

              <Separator orientation="vertical" className="mx-1 h-6" />

              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    type="button"
                    className="flex items-center gap-2 rounded-md px-1.5 py-1 hover:bg-accent"
                  >
                    <span className="grid h-7 w-7 place-items-center rounded-full bg-primary text-xs font-semibold text-primary-foreground">
                      {initials(user?.full_name ?? '?')}
                    </span>
                    <span className="hidden text-sm font-medium sm:inline">{user?.full_name}</span>
                    <ChevronDown className="h-4 w-4 text-muted-foreground" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-60">
                  <DropdownMenuLabel className="font-normal">
                    <p className="text-sm font-medium text-foreground">{user?.full_name}</p>
                    <p className="truncate text-xs text-muted-foreground">{user?.email}</p>
                    {user?.roles?.length ? (
                      <div className="mt-2 flex flex-wrap gap-1">
                        {user.roles.map((role) => (
                          <Badge key={role} variant="secondary" className="text-2xs">
                            {role}
                          </Badge>
                        ))}
                      </div>
                    ) : null}
                  </DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem asChild>
                    <Link to="/profile">
                      <UserIcon />
                      Profile & sessions
                    </Link>
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem destructive onSelect={() => void logout()}>
                    <LogOut />
                    Sign out
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </header>

          <main className="min-w-0 flex-1 overflow-y-auto bg-muted/30 p-4 lg:p-6">
            <Outlet />
          </main>
        </div>
      </div>
    </TooltipProvider>
  )
}

function SidebarLink({ item, collapsed }: { item: NavItem; collapsed: boolean }) {
  const Icon = item.icon
  return (
    <li>
      <NavLink
        to={item.to}
        // `end` on the dashboard only: without it "/" would match every route.
        end={item.to === '/'}
        className={({ isActive }) =>
          cn(
            'flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors',
            isActive
              ? 'bg-sidebar-accent font-medium text-white'
              : 'text-sidebar-foreground/80 hover:bg-white/5 hover:text-white',
            collapsed && 'justify-center px-0',
          )
        }
        title={collapsed ? item.label : undefined}
      >
        <Icon className="h-4 w-4 shrink-0" aria-hidden />
        {!collapsed && <span className="truncate">{item.label}</span>}
      </NavLink>
    </li>
  )
}

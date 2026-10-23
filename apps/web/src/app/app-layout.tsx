import { describeError } from '@/lib/messages'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { useQuery } from '@tanstack/react-query'
import {
  Bell,
  ChevronDown,
  LogOut,
  Menu,
  PanelLeftClose,
  Search,
  User as UserIcon,
  X,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'

import { navigation, type NavItem, type NavSection } from '@/app/navigation'
import { prefetchRoute } from '@/app/route-loaders'
import { Button } from '@/components/ui/button'
// Imported from the specific modules, not the `overlays` barrel: the barrel
// re-exports six Radix families, and every component in them is a top-level
// `forwardRef(...)` call, which Rollup cannot prove side-effect-free and so
// cannot tree-shake away. Going through it from the eager shell therefore
// pulled Select, Dialog, Popover and Tabs into the entry chunk every visitor
// downloads before anything renders. Lazy screens may keep using the barrel.
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Badge, Separator } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { api } from '@/lib/api-client'
import { NOTIFICATION_POLL_MS, STORAGE_KEYS } from '@/lib/constants'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { ThemeToggle } from '@/components/theme-toggle'
import { cn, initials } from '@/lib/utils'
import type { SchemaOf } from '@/types/api'

const SIDEBAR_KEY = STORAGE_KEYS.sidebar

export function AppLayout() {
  const { user, logout, can, canAny } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const [mobileOpen, setMobileOpen] = useState(false)
  const [searchTerm, setSearchTerm] = useState('')
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(SIDEBAR_KEY) === 'collapsed',
  )

  // Close the mobile drawer on navigation, otherwise it covers the page the
  // user just asked for.
  useEffect(() => setMobileOpen(false), [location.pathname])

  // A drawer opened on a phone must release its focus/scroll lock when the
  // viewport grows into the persistent desktop sidebar.
  useEffect(() => {
    const desktop = window.matchMedia('(min-width: 1024px)')
    const onChange = () => {
      if (desktop.matches) setMobileOpen(false)
    }
    desktop.addEventListener('change', onChange)
    return () => desktop.removeEventListener('change', onChange)
  }, [])

  useEffect(() => {
    localStorage.setItem(SIDEBAR_KEY, collapsed ? 'collapsed' : 'expanded')
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
  // The one search the shell can honour: it opens the items list on the term
  // (`useListQuery` reads `q` from the URL), so it is offered only to someone
  // who may open that list.
  const maySearchItems = can(P.ITEM_VIEW)
  const unread = useQuery({
    queryKey: queryKeys.notifications.unreadCount,
    queryFn: () => api.get<SchemaOf<'UnreadCountRead'>>('/notifications/unread-count'),
    enabled: maySeeNotifications,
    refetchInterval: NOTIFICATION_POLL_MS,
    staleTime: NOTIFICATION_POLL_MS / 2,
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
            'hidden w-64 shrink-0 flex-col bg-sidebar text-sidebar-foreground transition-all duration-200 lg:flex',
            collapsed && 'lg:w-[68px]',
          )}
        >
          <SidebarContent
            companyName={user?.company_name}
            sections={visibleSections}
            collapsed={collapsed}
            onToggle={() => setCollapsed((c) => !c)}
          />
        </aside>

        {/* ---------------------------------------------------- main area -- */}
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-20 flex h-14 shrink-0 items-center gap-3 border-b bg-background/95 px-4 backdrop-blur">
            <MobileNavigation
              open={mobileOpen}
              onOpenChange={setMobileOpen}
              companyName={user?.company_name}
              sections={visibleSections}
            />

            {maySearchItems && (
              <form
                role="search"
                className="relative hidden min-w-0 max-w-md flex-1 md:block"
                onSubmit={(event) => {
                  event.preventDefault()
                  const term = searchTerm.trim()
                  if (term) navigate(`/items?q=${encodeURIComponent(term)}`)
                }}
              >
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <input
                  type="search"
                  value={searchTerm}
                  onChange={(event) => setSearchTerm(event.target.value)}
                  placeholder="Search items..."
                  aria-label="Search items"
                  className="h-9 w-full rounded-md border border-input bg-background pl-8 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </form>
            )}

            <div className="ml-auto flex items-center gap-1">
              {/* Light / Dark / System. Every state is one click away and
                  visibly current, which a single cycling button cannot manage
                  with three states. */}
              <ThemeToggle />

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
                    onMouseEnter={() => prefetchRoute('/notifications')}
                    onFocus={() => prefetchRoute('/notifications')}
                    className="relative"
                    title={
                      unreadUnavailable
                        ? `The unread count could not be loaded: ${describeError(unread.error)}`
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
                    <span
                      className="hidden max-w-40 truncate text-sm font-medium sm:inline"
                      title={user?.full_name}
                    >
                      {user?.full_name}
                    </span>
                    <ChevronDown className="h-4 w-4 text-muted-foreground" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-60">
                  <DropdownMenuLabel className="font-normal">
                    <p className="break-words text-sm font-medium text-foreground">
                      {user?.full_name}
                    </p>
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
                    <Link
                      to="/profile"
                      onMouseEnter={() => prefetchRoute('/profile')}
                      onFocus={() => prefetchRoute('/profile')}
                    >
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

function MobileNavigation({
  open,
  onOpenChange,
  companyName,
  sections,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  companyName?: string | null
  sections: NavSection[]
}) {
  // CSS translation alone left invisible links in the keyboard order of every
  // phone screen. A modal contains focus and returns it to the opener on close.
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Trigger asChild>
        <button
          type="button"
          className="rounded-md p-1.5 hover:bg-accent lg:hidden"
          aria-label="Open navigation"
        >
          <Menu className="h-5 w-5" />
        </button>
      </DialogPrimitive.Trigger>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-30 bg-black/50" />
        <DialogPrimitive.Content
          className="fixed inset-y-0 left-0 z-40 flex w-64 max-w-[calc(100vw-2rem)] flex-col bg-sidebar text-sidebar-foreground outline-none"
          aria-describedby={undefined}
        >
          <DialogPrimitive.Title className="sr-only">Main navigation</DialogPrimitive.Title>
          <SidebarContent companyName={companyName} sections={sections} mobile />
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

function SidebarContent({
  companyName,
  sections,
  collapsed = false,
  mobile = false,
  onToggle,
}: {
  companyName?: string | null
  sections: NavSection[]
  collapsed?: boolean
  mobile?: boolean
  onToggle?: () => void
}) {
  return (
    <>
      <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-sidebar-border px-4">
        <div className="grid h-8 w-8 shrink-0 place-items-center rounded-md bg-sidebar-accent font-bold text-white">
          A
        </div>
        <div className={cn('min-w-0 flex-1', collapsed && 'lg:hidden')}>
          <p className="truncate text-sm font-semibold text-white">Atcraft ERP</p>
          <p className="truncate text-2xs text-sidebar-foreground/70">{companyName ?? ''}</p>
        </div>
        {mobile && (
          <DialogPrimitive.Close asChild>
            <button
              type="button"
              className="ml-auto rounded p-1 text-sidebar-foreground/70 hover:text-white"
              aria-label="Close navigation"
            >
              <X className="h-5 w-5" />
            </button>
          </DialogPrimitive.Close>
        )}
      </div>

      <nav className="min-h-0 flex-1 space-y-4 overflow-y-auto px-2.5 py-4" aria-label="Main">
        {sections.map((section) => (
          <div key={section.label}>
            <p
              className={cn(
                'px-2.5 pb-1.5 text-2xs font-semibold uppercase tracking-wider text-sidebar-foreground/70',
                collapsed && 'lg:hidden',
              )}
            >
              {section.label}
            </p>
            <ul className="space-y-0.5">
              {section.items.map((item) => (
                <SidebarLink key={item.to} item={item} collapsed={collapsed} />
              ))}
            </ul>
          </div>
        ))}
      </nav>

      {!mobile && (
        <div className="border-t border-sidebar-border p-2.5">
          <button
            type="button"
            onClick={onToggle}
            className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-sidebar-foreground/70 hover:bg-white/5 hover:text-white"
            aria-label={collapsed ? 'Expand navigation' : 'Collapse navigation'}
          >
            <PanelLeftClose
              className={cn('h-4 w-4 transition-transform', collapsed && 'rotate-180')}
            />
            {!collapsed && <span>Collapse</span>}
          </button>
        </div>
      )}
    </>
  )
}

function SidebarLink({ item, collapsed }: { item: NavItem; collapsed: boolean }) {
  const Icon = item.icon
  // Warm the screen's chunk on intent - pointer over, keyboard focus, or the
  // touch that precedes a tap - so the click that follows paints from cache
  // instead of showing the route spinner. Idempotent; see route-loaders.ts.
  const warm = () => prefetchRoute(item.to)
  return (
    <li>
      <NavLink
        to={item.to}
        onMouseEnter={warm}
        onFocus={warm}
        onTouchStart={warm}
        // `end` on the dashboard only: without it "/" would match every route.
        end={item.to === '/'}
        className={({ isActive }) =>
          cn(
            'flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors',
            isActive
              ? 'bg-sidebar-accent font-medium text-white'
              : 'text-sidebar-foreground/80 hover:bg-white/5 hover:text-white',
            collapsed && 'lg:justify-center lg:px-0',
          )
        }
        title={collapsed ? item.label : undefined}
      >
        <Icon className="h-4 w-4 shrink-0" aria-hidden />
        {/* sr-only rather than hidden when collapsed: the icon-only desktop
            link keeps its accessible name, and the drawer keeps its label. */}
        <span className={cn('truncate', collapsed && 'lg:sr-only')}>{item.label}</span>
      </NavLink>
    </li>
  )
}

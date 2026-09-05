/**
 * The permission matrix for one role.
 *
 * The catalogue is a compiled-in vocabulary served whole by
 * `GET /roles/permissions/catalogue` - a few hundred fixed strings, grouped in
 * the order the registry declares them. It is not paginated and must not be
 * filtered on the server: the whole point of a matrix is seeing every cell at
 * once, and the search box below narrows what is *rendered*, never what was
 * fetched.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Save, Search, ShieldAlert, Undo2, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

import { EmptyState } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Checkbox,
  Input,
  Label,
  Skeleton,
} from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { fieldErrorSummary } from '@/lib/form-errors'
import { formatNumber } from '@/lib/format'
import { success } from '@/lib/messages'
import { queryKeys, staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import { isRoleImmutable, usePermissionCatalogue, type RoleDetail } from './admin-types'

function sameSet(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false
  for (const value of a) if (!b.has(value)) return false
  return true
}

export function PermissionMatrix({
  roleId,
  canManage,
}: {
  roleId: string | null
  /** Affordance only - `role.manage` is re-checked by the API on every save. */
  canManage: boolean
}) {
  const queryClient = useQueryClient()
  const catalogue = usePermissionCatalogue(Boolean(roleId))
  const [term, setTerm] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [saveError, setSaveError] = useState<string | null>(null)

  const role = useQuery({
    queryKey: [...queryKeys.users.roles, 'detail', roleId] as const,
    queryFn: () => api.get<RoleDetail>(`/roles/${roleId}`),
    enabled: Boolean(roleId),
    staleTime: staleTimes.master,
  })

  const stored = useMemo(() => new Set(role.data?.permissions ?? []), [role.data])

  // Seeded from the server whenever a different role is loaded. Keyed on the id
  // rather than the object so a background refetch cannot discard edits in
  // progress.
  useEffect(() => {
    if (role.data) setSelected(new Set(role.data.permissions ?? []))
  }, [role.data?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const mutation = useMutation({
    mutationFn: (permissions: string[]) =>
      api.patch<RoleDetail>(`/roles/${roleId}`, { permissions }),
    onMutate: () => setSaveError(null),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.users.all })
      toast.success(
        success.saved('Permissions'),
        `${saved.name} now grants ${saved.permission_count} permissions`,
      )
    },
    onError: (error) => {
      // The whole grid is one field, so there is no control to hang a 422 on;
      // it is shown above the grid instead. The mutation cache stays quiet for
      // a 422 with field detail on the understanding that the screen shows it.
      const message = fieldErrorSummary(error)
      if (message) setSaveError(message)
    },
  })

  const readOnly = !canManage || (role.data ? isRoleImmutable(role.data) : true)
  const dirty = !sameSet(selected, stored)

  const groups = useMemo(() => {
    const needle = term.trim().toLowerCase()
    // A group with nothing in it arrives without its `permissions` key at all,
    // so it is defaulted before anything walks it.
    return (catalogue.data?.groups ?? [])
      .map((group) => {
        const permissions = group.permissions ?? []
        return {
          ...group,
          permissions: needle
            ? permissions.filter(
                (entry) =>
                  entry.value.toLowerCase().includes(needle) ||
                  entry.resource.toLowerCase().includes(needle) ||
                  entry.action.toLowerCase().includes(needle) ||
                  group.group.toLowerCase().includes(needle),
              )
            : permissions,
        }
      })
      .filter((group) => group.permissions.length > 0)
  }, [catalogue.data, term])

  if (!roleId) {
    return (
      <EmptyState
        icon={ShieldAlert}
        title="Select a role"
        description="Pick a role on the Roles tab to see and edit exactly what it grants."
      />
    )
  }

  if (role.isLoading || catalogue.isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-9 w-64" />
        {Array.from({ length: 4 }).map((_, index) => (
          <Skeleton key={index} className="h-28 w-full" />
        ))}
      </div>
    )
  }

  if (role.error || catalogue.error) {
    const error = (role.error ?? catalogue.error) as Error
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load the permission matrix</AlertTitle>
        <AlertDescription>{error.message}</AlertDescription>
      </Alert>
    )
  }

  const toggle = (value: string, on: boolean) => {
    setSelected((current) => {
      const next = new Set(current)
      if (on) next.add(value)
      else next.delete(value)
      return next
    })
  }

  const toggleGroup = (values: string[], on: boolean) => {
    setSelected((current) => {
      const next = new Set(current)
      for (const value of values) {
        if (on) next.add(value)
        else next.delete(value)
      }
      return next
    })
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 className="flex flex-wrap items-center gap-2 text-base font-semibold">
            {role.data?.name}
            <span className="font-mono text-xs font-normal text-muted-foreground">
              {role.data?.code}
            </span>
            {role.data?.is_system && <Badge variant="secondary">System</Badge>}
            {role.data?.is_vendor_role && <Badge variant="muted">Vendor</Badge>}
          </h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {formatNumber(selected.size)} of {formatNumber(catalogue.data?.total ?? 0)} permissions
            selected · held by {formatNumber(role.data?.user_count ?? 0)} user(s)
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {dirty && !readOnly && (
            <Button variant="ghost" size="sm" onClick={() => setSelected(new Set(stored))}>
              <Undo2 />
              Discard
            </Button>
          )}
          {!readOnly && (
            <Button
              size="sm"
              disabled={!dirty}
              loading={mutation.isPending}
              onClick={() => mutation.mutate([...selected].sort())}
            >
              <Save />
              Save permissions
            </Button>
          )}
        </div>
      </div>

      <p className="text-xs text-muted-foreground">
        These checkboxes change what the API allows, not merely what this application shows: every
        request from a holder of this role is authorised against the saved set.
      </p>

      {readOnly && (
        <Alert variant="warning">
          <AlertTitle>Read-only</AlertTitle>
          <AlertDescription>
            {role.data && isRoleImmutable(role.data)
              ? 'The platform administration role cannot be re-permissioned - editing it would let one administrator lock out every other.'
              : 'You do not hold the permission to change a role. The matrix below is what this role currently grants.'}
          </AlertDescription>
        </Alert>
      )}

      {saveError && (
        <Alert variant="destructive">
          <AlertTitle>The permissions were not saved</AlertTitle>
          <AlertDescription>{saveError}</AlertDescription>
        </Alert>
      )}

      <div className="relative max-w-sm">
        <Search
          className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden
        />
        <Input
          value={term}
          onChange={(event) => setTerm(event.target.value)}
          placeholder="Filter permissions..."
          aria-label="Filter permissions"
          className="pl-8 pr-8"
        />
        {term && (
          <button
            type="button"
            onClick={() => setTerm('')}
            className="absolute right-2 top-1/2 -translate-y-1/2 rounded-sm text-muted-foreground hover:text-foreground"
            aria-label="Clear permission filter"
          >
            <X className="h-4 w-4" />
          </button>
        )}
      </div>

      {groups.length === 0 ? (
        <EmptyState
          icon={Search}
          title="No permission matches"
          description="Nothing in the registry matches that text. Try a resource name such as 'purchase_order'."
        />
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {groups.map((group) => {
            const values = group.permissions.map((entry) => entry.value)
            const chosen = values.filter((value) => selected.has(value)).length
            const groupState =
              chosen === 0 ? false : chosen === values.length ? true : ('indeterminate' as const)
            const groupId = `group-${group.group.replace(/\W+/g, '-').toLowerCase()}`

            return (
              <Card key={group.group}>
                <CardHeader className="flex-row flex-wrap items-center justify-between gap-3 space-y-0 pb-3">
                  <CardTitle className="min-w-0 text-sm">{group.group}</CardTitle>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-muted-foreground">
                      {chosen}/{values.length}
                    </span>
                    <Checkbox
                      id={groupId}
                      checked={groupState}
                      disabled={readOnly}
                      onCheckedChange={(next) => toggleGroup(values, next === true)}
                    />
                    <Label htmlFor={groupId} className="text-xs text-muted-foreground">
                      All
                    </Label>
                  </div>
                </CardHeader>
                <CardContent className="grid gap-1.5 sm:grid-cols-2">
                  {group.permissions.map((entry) => {
                    const inputId = `perm-${entry.value.replace(/\W+/g, '-')}`
                    return (
                      <div key={entry.value} className="flex items-start gap-2">
                        <Checkbox
                          id={inputId}
                          checked={selected.has(entry.value)}
                          disabled={readOnly}
                          onCheckedChange={(next) => toggle(entry.value, next === true)}
                          className="mt-0.5"
                        />
                        <Label
                          htmlFor={inputId}
                          className="min-w-0 cursor-pointer text-xs font-normal leading-tight"
                        >
                          <span className="block font-medium capitalize">
                            {entry.action.replace(/_/g, ' ')}
                          </span>
                          <span
                            className="block truncate font-mono text-2xs text-muted-foreground"
                            title={entry.value}
                          >
                            {entry.value}
                          </span>
                        </Label>
                      </div>
                    )
                  })}
                </CardContent>
              </Card>
            )
          })}
        </div>
      )}
    </div>
  )
}

/**
 * Wire shapes for the identity and organisation screens, plus the small lookup
 * hooks both of them need.
 *
 * These mirror `src/modules/users/schemas.py` and
 * `src/modules/organization/schemas.py`. Where a field is absent from an update
 * payload it is absent here too, and the reason is recorded next to it: the API
 * refuses several of them outright, and a form that offers a control the server
 * will reject wastes the whole entry.
 */

import { useQuery } from '@tanstack/react-query'

import {
  useBranchOptions as useSharedBranchOptions,
  useDepartmentOptions as useSharedDepartmentOptions,
  usePlantOptions as useSharedPlantOptions,
} from '@/hooks/use-organisation-options'
import { api } from '@/lib/api-client'
import { queryKeys, staleTimes } from '@/lib/query-client'

// ------------------------------------------------------------- identity -----

/** One row of `GET /users`. */
export interface UserRow {
  id: string
  email: string
  full_name: string
  employee_code: string | null
  is_active: boolean
  is_superuser: boolean
  must_change_password: boolean
  vendor_id: string | null
  branch_id: string | null
  plant_id: string | null
  department_id: string | null
  last_login_at: string | null
  locked_until: string | null
  created_at: string
  /** Omitted, not emptied, when the user holds no role. */
  role_codes?: string[]
}

/**
 * `GET /users/{id}` - the grid deliberately does not carry these columns.
 *
 * Every collection here is omitted rather than emptied when it has nothing in
 * it, so none of them may be dereferenced without a default.
 */
export interface UserDetail extends UserRow {
  phone: string | null
  mfa_enabled: boolean
  failed_login_attempts: number
  password_changed_at: string | null
  allowed_category_ids?: string[]
  allowed_warehouse_ids?: string[]
  extra_permissions?: string[]
  denied_permissions?: string[]
  is_vendor_user?: boolean
  updated_at: string
  roles?: RoleRow[]
}

/** One row of `GET /roles`. */
export interface RoleRow {
  id: string
  code: string
  name: string
  description: string | null
  is_system: boolean
  is_vendor_role: boolean
  is_active: boolean
  permission_count: number
  created_at: string
}

/** `GET /roles/{id}` - carries the permission set the matrix edits. */
export interface RoleDetail extends RoleRow {
  /** Omitted, not emptied, on a role that grants nothing. */
  permissions?: string[]
  /** Live holders. Drives the "N users are affected" warning before a save. */
  user_count?: number
  updated_at: string
}

export interface PermissionEntry {
  value: string
  resource: string
  action: string
  group: string
}

export interface PermissionGroup {
  group: string
  resources?: string[]
  permissions?: PermissionEntry[]
}

/** `GET /roles/permissions/catalogue` - a fixed vocabulary, never paginated. */
export interface PermissionCatalogue {
  groups?: PermissionGroup[]
  total?: number
}

export interface PasswordResetResult {
  user_id: string
  must_change_password: boolean
  sessions_revoked: number
  /** Present only when the server generated one. Shown once, never recoverable. */
  temporary_password?: string | null
}

/**
 * The one role the API refuses to re-permission.
 *
 * Mirrors `IMMUTABLE_ROLE_CODES` in `src/modules/users/service.py`. Editing the
 * platform-administration role would let an administrator lock every other
 * administrator out of the system with a single save.
 */
export const IMMUTABLE_ROLE_CODES = new Set(['SUPER_ADMIN'])

export function isRoleImmutable(role: Pick<RoleRow, 'code'>): boolean {
  return IMMUTABLE_ROLE_CODES.has(role.code)
}

// --------------------------------------------------------- organisation -----

export interface CompanyRecord {
  id: string
  code: string
  name: string
  legal_name: string | null
  gstin: string | null
  pan: string | null
  cin: string | null
  email: string | null
  phone: string | null
  website: string | null
  address_line1: string | null
  address_line2: string | null
  city: string | null
  state: string | null
  state_code: string | null
  postal_code: string | null
  country: string
  currency: string
  financial_year_start_month: number
  timezone: string
  is_active: boolean
  logo_document_id: string | null
  created_at: string
  updated_at: string
}

export interface BranchRow {
  id: string
  code: string
  name: string
  gstin: string | null
  address_line1: string | null
  city: string | null
  state: string | null
  state_code: string | null
  postal_code: string | null
  phone: string | null
  email: string | null
  is_active: boolean
  is_head_office: boolean
  created_at: string
  updated_at: string
  /** Omitted rather than nulled on a live row. */
  deleted_at?: string | null
}

export interface PlantRow {
  id: string
  code: string
  name: string
  branch_id: string | null
  address_line1: string | null
  city: string | null
  state: string | null
  is_active: boolean
  created_at: string
  updated_at: string
  /** Omitted rather than nulled on a live row. */
  deleted_at?: string | null
}

export interface DepartmentRow {
  id: string
  code: string
  name: string
  plant_id: string | null
  parent_id: string | null
  head_user_id: string | null
  is_active: boolean
  created_at: string
  updated_at: string
  /** Omitted rather than nulled on a live row. */
  deleted_at?: string | null
}

export interface FinancialYearRow {
  id: string
  code: string
  start_date: string
  end_date: string
  is_active: boolean
  is_closed: boolean
  notes: string | null
  created_at: string
  updated_at: string
  /** Omitted rather than nulled on a live row. */
  deleted_at?: string | null
}

/**
 * Organisation query keys.
 *
 * The shared registry in `lib/query-client.ts` has no organisation entry, so
 * these live with the feature that owns them. They share the `organization`
 * root used by the option hooks elsewhere in the app, so one invalidation of
 * `orgKeys.all` clears every branch and plant picker too.
 */
export const orgKeys = {
  all: ['organization'] as const,
  company: ['organization', 'company'] as const,
  branches: (params: unknown) => ['organization', 'branches', params] as const,
  plants: (params: unknown) => ['organization', 'plants', params] as const,
  departments: (params: unknown) => ['organization', 'departments', params] as const,
  financialYears: (params: unknown) => ['organization', 'financial-years', params] as const,
} as const

// ---------------------------------------------------------------- lookups ---

/**
 * The placement lookups, typed to the fuller rows this feature renders.
 *
 * The request itself lives in `hooks/use-organisation-options` - one query key
 * per list, shared with the warehouse and production screens that need the same
 * names. The type argument only says which of the returned fields this feature
 * reads; it does not change what is fetched.
 */
export function useBranchOptions(enabled = true) {
  return useSharedBranchOptions<BranchRow>(enabled)
}

export function usePlantOptions(enabled = true) {
  return useSharedPlantOptions<PlantRow>(enabled)
}

export function useDepartmentOptions(enabled = true) {
  return useSharedDepartmentOptions<DepartmentRow>(enabled)
}

/** Every role, for the assignment checklist. Bounded by design. */
export function useRoleOptions(enabled = true) {
  return useQuery({
    queryKey: queryKeys.users.roles,
    queryFn: async () => {
      const page = await api.getPage<RoleRow>('/roles', {
        params: {
          limit: 200,
          is_active: true,
          sort_by: 'name',
          sort_dir: 'asc',
          with_total: false,
        },
      })
      return page.data
    },
    staleTime: staleTimes.reference,
    enabled,
  })
}

/**
 * Staff names for the "head of department" picker.
 *
 * A directory is bounded in a way the transactional tables are not, so one page
 * of two hundred is the whole list for any realistic company. A caller without
 * `user.view` gets an empty list and a select that offers only "unassigned",
 * rather than a broken screen.
 */
export function useUserOptions(enabled = true) {
  return useQuery({
    queryKey: ['users', 'options'] as const,
    queryFn: async (): Promise<UserRow[]> => {
      try {
        const page = await api.getPage<UserRow>('/users', {
          params: {
            limit: 200,
            is_active: true,
            sort_by: 'full_name',
            sort_dir: 'asc',
            with_total: false,
          },
        })
        return page.data
      } catch {
        return []
      }
    },
    staleTime: staleTimes.reference,
    enabled,
  })
}

export function usePermissionCatalogue(enabled = true) {
  return useQuery({
    queryKey: queryKeys.users.permissionCatalogue,
    queryFn: () => api.get<PermissionCatalogue>('/roles/permissions/catalogue'),
    // Compiled into the backend binary; it cannot change while a tab is open.
    staleTime: staleTimes.reference,
    enabled,
  })
}

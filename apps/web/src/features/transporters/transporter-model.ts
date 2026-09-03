/**
 * Wire shapes, vocabulary and lifecycle for the transporter master.
 *
 * A transporter is the carrier a dispatch is handed to - a freight transporter
 * or a courier. The only field on the record that does any work afterwards is
 * `tracking_url_template`: the consignment number keyed on a dispatch is
 * substituted into it, so the customer is given a link rather than a number.
 *
 * Three properties of this contract shape every screen above it.
 *
 * 1. **Active/inactive and retired are two axes, not three states of one.**
 *    `is_active` says whether the carrier may be named on a new dispatch;
 *    `deleted_at` says the master row has been retired. The list endpoint
 *    exposes them as two independent filters, and `include_deleted=true` *adds*
 *    retired rows to the result rather than showing only those - there is no
 *    "retired only" narrowing to offer. Retirement also clears `is_active`, so
 *    a retired carrier never appears under `is_active=true`.
 *
 * 2. **Every by-id endpoint but one excludes a retired row.** The repository's
 *    `get_or_404` is scoped to undeleted rows, so the detail, tracking-url,
 *    PATCH, activate, deactivate and DELETE endpoints all answer 404 for a
 *    retired carrier. `POST /restore` is the single exception - it looks the row
 *    up with `include_deleted=True`. That is why a retired row offers exactly
 *    one action and cannot be opened for editing.
 *
 * 3. **Restore brings the carrier back inactive, never active.**
 *    `TRANSPORTER_LIFECYCLE` has an ARCHIVED -> INACTIVE edge and none from
 *    ARCHIVED to ACTIVE, so coming back is two deliberate steps: restore, then
 *    activate. The API's own success message says "Transporter restored,
 *    inactive"; the UI has to say the same or the carrier looks usable when it
 *    is not.
 *
 * Nothing here is money or a quantity - the contract carries no rate, no
 * freight charge and no weight - so no decimal arithmetic happens on these
 * screens.
 */

import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api-client'
import { P, type PermissionKey } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

// ------------------------------------------------------------ query keys ----

/**
 * The shared registry in `lib/query-client` has no transporter branch, so the
 * keys live here in the same hierarchical shape as `assemblyKeys`: invalidating
 * `transporterKeys.all` clears every list, the picker options and any detail at
 * once. A lifecycle command needs exactly that - activating a carrier changes
 * both the list it is listed in and the option list the dispatch screen reads.
 */
export const transporterKeys = {
  all: ['transporters'] as const,
  list: (params: unknown) => ['transporters', 'list', params] as const,
  detail: (id: string) => ['transporters', 'detail', id] as const,
  options: (transporterType: TransporterType | null) =>
    ['transporters', 'options', transporterType] as const,
  /**
   * Keyed by the consignment number as well as by the carrier: two numbers
   * resolved against the same transporter are different answers and must not
   * read one another's cache entry.
   */
  trackingLink: (id: string, trackingId: string) =>
    ['transporters', 'detail', id, 'tracking-link', trackingId] as const,
}

// ----------------------------------------------------------- wire shapes ----

/** One row of `GET /transporters` and the body of `GET /transporters/{id}`. */
export type Transporter = SchemaOf<'TransporterRead'>

/**
 * One entry of `GET /transporters/options`.
 *
 * Not a trimmed `TransporterRead`: it carries `has_tracking`, which the read
 * model does not, so a picker can say whether choosing this carrier will
 * produce a working link without fetching each row's template.
 */
export type TransporterOption = SchemaOf<'TransporterOption'>

export type TransporterType = SchemaOf<'TransporterType'>

export type TransporterCreateBody = SchemaOf<'TransporterCreate'>

/**
 * `PATCH /transporters/{id}`.
 *
 * `code` and `is_active` are absent by design and the model is `extra="forbid"`,
 * so sending either is a 422 rather than a silently ignored field. The code is
 * immutable once dispatches quote it, and activation is its own endpoint with
 * its own audited reason.
 */
export type TransporterUpdateBody = SchemaOf<'TransporterUpdate'>

/** `GET /transporters/{id}/tracking-url`. */
export type TrackingLink = SchemaOf<'TrackingLinkRead'>

/**
 * The body activate and deactivate insist on.
 *
 * Four modules define a `StatusChangeRequest`, so the generated contract
 * disambiguates them by Python module path; this is the vendors module's one.
 */
export type TransporterStatusChange = SchemaOf<'src__modules__vendors__schemas__StatusChangeRequest'>

// ------------------------------------------------------------ vocabulary ----

/**
 * Every carrier shape with the label the UI shows.
 *
 * A `Record` keyed on the contract union rather than a hand-kept array: a third
 * carrier shape added to the API stops the build here instead of quietly
 * disappearing from the filter and the form.
 */
export const TRANSPORTER_TYPE_LABELS: Record<TransporterType, string> = {
  TRANSPORT: 'Transport',
  COURIER: 'Courier',
}

/** Cast to a non-empty tuple so `z.enum` accepts it without a second list. */
export const TRANSPORTER_TYPES = Object.keys(TRANSPORTER_TYPE_LABELS) as [
  TransporterType,
  ...TransporterType[],
]

// ------------------------------------------------------------- lifecycle ----

/**
 * `TransporterState` in the service, derived rather than stored.
 *
 * There is no status column: retirement wins over deactivation, exactly as
 * `transporter_state()` decides it, so a retired row that still carried
 * `is_active=True` from before could never read as usable. (It cannot: the
 * delete path clears the flag. The order still matters, because the list is the
 * one place a stale row can be looked at.)
 */
export type TransporterState = 'ACTIVE' | 'INACTIVE' | 'RETIRED'

export const TRANSPORTER_STATE_LABELS: Record<TransporterState, string> = {
  ACTIVE: 'Active',
  INACTIVE: 'Inactive',
  RETIRED: 'Retired',
}

export function transporterState(transporter: Transporter): TransporterState {
  // `deleted_at` is optional in the contract, not merely nullable, so an
  // endpoint that omits it must read as "not retired" rather than as undefined.
  if (transporter.deleted_at) return 'RETIRED'
  return transporter.is_active ? 'ACTIVE' : 'INACTIVE'
}

/**
 * Whether the edit form can be opened on this row at all.
 *
 * Not a permission question: `PATCH /transporters/{id}` resolves the row with
 * `get_or_404`, which excludes retired rows, so an edit dialog opened on one
 * would 404 on load and 404 again on save. Restore it first.
 */
export const isEditable = (state: TransporterState): boolean => state !== 'RETIRED'

// -------------------------------------------------------------- commands ----

export type TransporterCommand = 'activate' | 'deactivate' | 'retire' | 'restore'

/** One row plus the command a confirmation dialog is collecting a reason for. */
export interface PendingTransporterCommand {
  transporter: Transporter
  command: TransporterCommand
}

export interface TransporterCommandCopy {
  /** The menu entry. */
  label: string
  title: string
  description: string
  confirmLabel: string
  destructive: boolean
  successTitle: string
  /** Said after the command, when the resulting state is not the obvious one. */
  successDescription?: string
  /**
   * The permission the *endpoint* enforces.
   *
   * All four are `transporter.manage`: this module splits its rights into view
   * and manage only, so there is no separate approval right to reach for. The
   * field exists anyway, so a route that later grows its own right is changed
   * here rather than in the four places that would otherwise have assumed.
   */
  permission: PermissionKey
  /**
   * Where the audited reason goes, which is not the same for all four.
   *
   * `body` - a `StatusChangeRequest` with a 3-255 character `reason`.
   * `query` - DELETE takes its reason as a **query parameter**, the odd one out
   * on this router.
   * `none` - restore records the status change from the transition itself and
   * accepts no payload, so asking for a reason would collect text that is
   * thrown away.
   */
  reason: 'body' | 'query' | 'none'
}

export const TRANSPORTER_COMMANDS: Record<TransporterCommand, TransporterCommandCopy> = {
  activate: {
    label: 'Activate',
    title: 'Activate this transporter?',
    description:
      'The carrier becomes selectable on new dispatches again. Consignments already with it are unaffected either way.',
    confirmLabel: 'Activate',
    destructive: false,
    successTitle: 'Transporter activated',
    permission: P.TRANSPORTER_MANAGE,
    reason: 'body',
  },
  deactivate: {
    label: 'Deactivate',
    title: 'Deactivate this transporter?',
    description:
      'The carrier disappears from the pickers on new dispatches. Nothing already booked to it moves, and its tracking links keep resolving.',
    confirmLabel: 'Deactivate',
    destructive: true,
    successTitle: 'Transporter deactivated',
    permission: P.TRANSPORTER_MANAGE,
    reason: 'body',
  },
  retire: {
    label: 'Retire',
    title: 'Retire this transporter?',
    description:
      'The master row is withdrawn and the code is freed. Refused while the carrier still holds an undelivered consignment, because the goods are physically with them. Retiring can be undone, but the carrier comes back inactive.',
    confirmLabel: 'Retire transporter',
    destructive: true,
    successTitle: 'Transporter retired',
    permission: P.TRANSPORTER_MANAGE,
    reason: 'query',
  },
  restore: {
    label: 'Restore',
    title: 'Restore this transporter?',
    description:
      'The retired carrier comes back, but inactive - activating it is a separate step. Refused if its code has since been taken by another transporter.',
    confirmLabel: 'Restore',
    destructive: false,
    successTitle: 'Transporter restored',
    successDescription: 'It is back as inactive. Activate it before a dispatch can name it.',
    permission: P.TRANSPORTER_MANAGE,
    reason: 'none',
  },
}

/**
 * The commands the API would actually accept in this state.
 *
 * Mirrors `TRANSPORTER_LIFECYCLE`: an active carrier has no `activate` edge, an
 * inactive one has no `deactivate` edge, and a retired one has only `restore` -
 * offering anything else on a retired row buys a 404 rather than a refusal
 * anyone can act on.
 */
export function commandsFor(state: TransporterState): TransporterCommand[] {
  switch (state) {
    case 'ACTIVE':
      return ['deactivate', 'retire']
    case 'INACTIVE':
      return ['activate', 'retire']
    case 'RETIRED':
      return ['restore']
  }
}

/** The shortest reason `StatusChangeRequest` and the delete query accept. */
export const MIN_REASON_LENGTH = 3
export const MAX_REASON_LENGTH = 255

/**
 * Run one lifecycle command.
 *
 * One function because the four endpoints disagree about where the reason goes
 * and what they answer with, and that knowledge belongs in a single place
 * rather than in a switch inside a component. `retire` resolves to `null`: the
 * DELETE answers `ApiResponse[None]`, so reading a code off it gives undefined.
 */
export async function runTransporterCommand(
  { transporter, command }: PendingTransporterCommand,
  reason: string,
): Promise<Transporter | null> {
  if (command === 'retire') {
    await api.delete(`/transporters/${transporter.id}`, { params: { reason } })
    return null
  }
  if (command === 'restore') {
    return api.post<Transporter>(`/transporters/${transporter.id}/restore`)
  }
  const body: TransporterStatusChange = { reason }
  return api.post<Transporter>(`/transporters/${transporter.id}/${command}`, body)
}

// -------------------------------------------------------------- tracking ----

/** The literal the server substitutes the consignment number into. */
export const TRACKING_PLACEHOLDER = '{tracking_id}'

export const TRACKING_TEMPLATE_MAX_LENGTH = 500

/**
 * Why a tracking template would be refused, or null if it would be accepted.
 *
 * Mirrors `_validate_tracking_template` so a typo is caught before the round
 * trip. Both rules are load-bearing rather than cosmetic: a template without
 * the placeholder resolves every consignment to the same page, which looks like
 * it works and never does, and the link is rendered to customers, so an
 * unencrypted carrier page is an easy downgrade target.
 */
export function trackingTemplateProblem(template: string): string | null {
  const trimmed = template.trim()
  if (trimmed === '') return null
  if (!trimmed.toLowerCase().startsWith('https://')) {
    return 'The tracking link must start with https://'
  }
  if (!trimmed.includes(TRACKING_PLACEHOLDER)) {
    return `The tracking link must contain the literal ${TRACKING_PLACEHOLDER} placeholder`
  }
  if (trimmed.length > TRACKING_TEMPLATE_MAX_LENGTH) {
    return `At most ${TRACKING_TEMPLATE_MAX_LENGTH} characters`
  }
  return null
}

/** The longest consignment number `tracking_id` accepts on the query string. */
export const MAX_TRACKING_ID_LENGTH = 64

/**
 * Whether a resolved tracking URL is safe to render as an anchor.
 *
 * The template is validated to be HTTPS on both create and update, so this can
 * only fail for a row stored before that validator existed. It is checked at the
 * point of rendering anyway, because the alternative is putting stored text
 * straight into an `href`.
 */
export const isHttpsUrl = (url: string | null): boolean =>
  url !== null && url.toLowerCase().startsWith('https://')

// --------------------------------------------------------------- options ----

export interface TransporterOptionsInput {
  /** Narrow to one carrier shape, as the endpoint's own filter does. */
  transporterType?: TransporterType | null
  enabled?: boolean
}

/**
 * Active transporters for a picker - the dispatch screen's list.
 *
 * `GET /transporters/options` is already bounded (500 rows, active only,
 * ordered by name) and returns a plain array rather than a page, so this is one
 * request and never a table pulled into the browser to be filtered there.
 *
 * A failure returns an empty list rather than propagating, for the same reason
 * `useBranchOptions` does: the caller may hold `dispatch.create` without
 * `transporter.view`, and an otherwise working dispatch screen should not be
 * replaced by an error state over a picker. Pass `enabled: false` when the
 * screen already knows the right is missing, so no request is made at all.
 */
export function useTransporterOptions({
  transporterType = null,
  enabled = true,
}: TransporterOptionsInput = {}) {
  return useQuery({
    queryKey: transporterKeys.options(transporterType),
    queryFn: async (): Promise<TransporterOption[]> => {
      try {
        return await api.get<TransporterOption[]>('/transporters/options', {
          params: transporterType ? { transporter_type: transporterType } : undefined,
        })
      } catch {
        return []
      }
    },
    staleTime: staleTimes.master,
    enabled,
  })
}

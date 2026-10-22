/**
 * The hand-written layer over the generated contract.
 *
 * `./openapi.ts` is generated from `../../openapi.json` by
 * `npm run contract:types` and is the source of truth for every request and
 * response shape. It is large, deeply nested and named after the OpenAPI
 * document, so this module is the thin, stable surface the app imports:
 * aliases into the generated schemas, plus the few things that are genuinely
 * client-side and have no server counterpart.
 *
 * The rule that makes this worth having: anything that describes what the
 * server sends is DERIVED here, never retyped. A retyped shape is a second
 * opinion, and a second opinion is how the ageing screen came to compile
 * perfectly while rendering nothing - `tsc` cannot see a disagreement between a
 * hand-written interface and a live API, only between two pieces of TypeScript.
 * Deriving turns that disagreement into a compile error.
 *
 * To change anything under `Schemas`, change the API and run `make contract`.
 */

import type { components } from './openapi'

/** Every schema in the contract, by its OpenAPI name. */
export type Schemas = components['schemas']

/** One schema by name: `SchemaOf<'AgeingReport'>`. */
export type SchemaOf<K extends keyof Schemas> = Schemas[K]

// -----------------------------------------------------------------------------
// Money and quantities
// -----------------------------------------------------------------------------

/**
 * A NUMERIC column, serialised as a string.
 *
 * The API sends money and quantities as decimal STRINGS on purpose: an IEEE
 * double cannot hold 0.1, and a rounding error in a stock ledger compounds.
 * Never `parseFloat` one - `lib/format.ts` converts through decimal.js.
 */
export type DecimalString = string

// -----------------------------------------------------------------------------
// Envelopes
// -----------------------------------------------------------------------------

/** `{ success, data, message }` - the shape every non-list endpoint returns. */
export type ApiEnvelope<T> = Omit<SchemaOf<'ApiResponse_AccessTokenResponse_'>, 'data'> & {
  /** Required after boundary validation; include null in T for nullable data. */
  data: T
}

/** Offset pagination metadata, exactly as the server defines it. */
export type OffsetPagination = SchemaOf<'OffsetPagination'>

/** Keyset pagination metadata, exactly as the server defines it. */
export type CursorPagination = SchemaOf<'CursorPagination'>

/** A paginated list: rows in `data`, metadata beside them. */
export type PaginatedEnvelope<T> = Omit<
  SchemaOf<'PaginatedResponse_ApprovalRequestRead_'>,
  'data' | 'pagination'
> & {
  data: T[]
  pagination: OffsetPagination
}

/**
 * A keyset-paginated list, used by the append-heavy tables (audit log,
 * inventory ledger, notifications) where OFFSET would drift under writes.
 */
export type CursorEnvelope<T> = Omit<
  SchemaOf<'CursorPaginatedResponse_AuditLogListItem_'>,
  'data' | 'pagination'
> & {
  data: T[]
  pagination: CursorPagination
}

/** A 202 from a long-running export: the work moved to Celery. */
export type AcceptedJob = SchemaOf<'AcceptedJobResponse'>

/** The error body every failing endpoint returns. */
export type ErrorResponse = SchemaOf<'ErrorResponse'>

// -----------------------------------------------------------------------------
// List query parameters
// -----------------------------------------------------------------------------

/**
 * The query string `useListQuery` builds. Client-side by nature - it is the
 * union of what the list endpoints accept, not a schema the server publishes,
 * so it is the one thing here that is legitimately hand-written.
 */
export interface ListParams {
  page?: number
  limit?: number
  sort_by?: string
  sort_dir?: 'asc' | 'desc'
  search?: string
  with_total?: boolean
  [key: string]: string | number | boolean | undefined | null
}

/**
 * Wire shapes and vocabulary for the document centre.
 *
 * A document is metadata plus bytes on a disk the browser can never address:
 * `DocumentRead` deliberately omits `storage_key`, so every byte is fetched
 * through `GET /documents/{id}/download` as an authenticated request. That is
 * why nothing in this feature builds an `<a href>` to a file.
 *
 * Five properties of the current contract shape the screens above it:
 *
 * 1. **A document is filed against something, not owned by it.** `entity_type`
 *    plus `entity_id` say what it is attached to, and there is no foreign key
 *    behind them - a QC certificate outlives the receipt it evidences. The pair
 *    is all or nothing, and `entity_id` alone is refused with a 422 rather than
 *    silently scanning the table, so the filters here send both or neither.
 * 2. **Expiry is not a status column - it is a horizon.** A row past
 *    `expires_at` still exists and still lists (with `include_expired`), but the
 *    API's metadata and download routes treat it as absent and answer 404. So an
 *    expired row is shown, and its download is not offered.
 * 3. **Deletion is the one write on this screen.** It takes its reason in the
 *    *query string* (`DELETE /documents/{id}?reason=...`, 3-255 characters),
 *    because several proxies drop a DELETE body. It is audited, it is
 *    irreversible, and it removes the row and the file together - there is no
 *    soft delete here on purpose: a row whose bytes are gone would advertise a
 *    file that 404s.
 * 4. **Rows are not denormalised.** The uploader arrives as an id with no name,
 *    and resolving one per row would be the N+1 the codebase forbids -
 *    `useUserOptions` fetches one page of users instead, and only for an
 *    account allowed to read them.
 * 5. **The upload allow-list is server configuration, not contract.** Neither
 *    the accepted MIME types nor the size ceiling appears in the OpenAPI
 *    document, so the constants below are a client-side convenience and the
 *    server stays the authority: it verifies the leading bytes against the
 *    declared type and rejects a mismatch whatever the extension says.
 */

import type { SchemaOf } from '@/types/api'

// ------------------------------------------------------------ query keys ----

/**
 * The shared registry in `lib/query-client` has no documents branch, so the keys
 * live here in the same hierarchical shape: invalidating `documentKeys.all`
 * clears every document list and detail at once.
 */
export const documentKeys = {
  all: ['documents'] as const,
  list: (params: unknown) => ['documents', 'list', params] as const,
  detail: (id: string) => ['documents', 'detail', id] as const,
}

// ----------------------------------------------------------- wire shapes ----

/**
 * One row of `GET /documents` and the whole of `GET /documents/{id}`
 * (`DocumentRead`).
 *
 * The list row and the detail are the same schema, which is why the detail
 * dialog can render immediately from the row it was opened on and use the
 * fetch only to confirm the file is still there.
 */
export type DocumentRow = SchemaOf<'DocumentRead'>

/** Storage class and default retention, not a permission (`DocumentCategory`). */
export type DocumentCategory = SchemaOf<'DocumentCategory'>

/**
 * The same vocabulary as a list, for the screens that offer it as options.
 *
 * `satisfies` is what keeps the two honest: a member the API has dropped stops
 * compiling here instead of surviving as a filter option nothing can match.
 */
export const DOCUMENT_CATEGORIES = [
  'DOCUMENT',
  'REPORT',
  'ATTACHMENT',
  'EXPORT',
  'TEMP',
] as const satisfies readonly DocumentCategory[]

/**
 * What the API will sort a document list by.
 *
 * Anything else comes back as a 400, so every other column header is fixed.
 */
export const SORTABLE_DOCUMENT_FIELDS = [
  'file_name',
  'size_bytes',
  'category',
  'created_at',
  'expires_at',
] as const

/** A column the API will accept as `sort_by`. */
export type SortableDocumentField = (typeof SORTABLE_DOCUMENT_FIELDS)[number]

/** One row of `GET /users` (`UserSummary`), used only to name an uploader. */
export type UploaderOption = SchemaOf<'UserSummary'>

/** The multipart form the upload endpoint binds. */
type UploadFields = SchemaOf<'Body_upload_document_api_v1_documents_post'>

/**
 * What the upload dialog collects.
 *
 * Not the generated body type: that types `file` as a string (OpenAPI's
 * `format: binary`) and cannot describe a `File`. The field *names* are still
 * taken from the contract - see `buildUploadBody`.
 */
export interface DocumentUploadInput {
  file: File
  category: DocumentCategory
  /** Both halves or neither; the API refuses half a reference. */
  entity_type: string | null
  entity_id: string | null
  is_public: boolean
  /** ISO-8601 **with an offset**. A naive timestamp is rejected outright. */
  expires_at: string | null
}

/**
 * Assemble the multipart body.
 *
 * `append` is keyed on the contract's own field names, so renaming one on the
 * server breaks the build here rather than producing an upload the API ignores.
 * Empty optionals are omitted rather than sent as "": FastAPI would bind the
 * empty string and fail validation on a field the user simply left alone.
 */
export function buildUploadBody(input: DocumentUploadInput): FormData {
  const body = new FormData()
  const append = (field: keyof UploadFields, value: string) => body.append(field, value)

  // The third argument is the part's filename - the only place the client's own
  // name for the file travels, and the server keeps it for display alone.
  body.append('file' satisfies keyof UploadFields, input.file, input.file.name)
  append('category', input.category)
  append('is_public', String(input.is_public))
  if (input.entity_type && input.entity_id) {
    append('entity_type', input.entity_type)
    append('entity_id', input.entity_id)
  }
  if (input.expires_at) append('expires_at', input.expires_at)

  return body
}

// ------------------------------------------------------------ attachment ----

/**
 * `entity_type` as the API validates it: lowercase snake_case, so the pair
 * stays a usable index key rather than a free-text dumping ground.
 */
export const ENTITY_TYPE_PATTERN = /^[a-z][a-z0-9_]*$/

/** `entity_id` is a UUID; anything else is a 422 before it reaches a query. */
export const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/**
 * The nouns this system actually files documents under today.
 *
 * Suggestions, not a closed set: any snake_case noun is accepted, and a module
 * that starts attaching files tomorrow will invent its own. Offering the known
 * ones saves typing without pretending the list is exhaustive.
 */
export const KNOWN_ENTITY_TYPES = ['qc_inspection', 'packing_order', 'report_snapshot'] as const

// ----------------------------------------------------------- file types -----

/**
 * The types the server can *prove* a file to be.
 *
 * Mirrors `KNOWN_TYPES` in `apps/api/src/modules/documents/service.py`: an
 * upload must be on the operator's allow-list **and** have a signature this
 * table knows, so nothing outside it can ever be stored - which is why it is
 * also the complete vocabulary for the type filter. The operator's list may be
 * narrower still; the contract publishes neither, so a filter option that
 * matches nothing simply returns an empty page.
 */
export const FILE_KINDS = {
  'application/pdf': { label: 'PDF', kind: 'pdf' },
  'image/jpeg': { label: 'JPEG image', kind: 'image' },
  'image/png': { label: 'PNG image', kind: 'image' },
  'image/webp': { label: 'WebP image', kind: 'image' },
  'text/csv': { label: 'CSV', kind: 'sheet' },
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': {
    label: 'Excel workbook',
    kind: 'sheet',
  },
} as const satisfies Record<string, { label: string; kind: 'pdf' | 'image' | 'sheet' }>

export type FileKind = (typeof FILE_KINDS)[keyof typeof FILE_KINDS]['kind'] | 'other'

/** The `accept` hint on the file input. Convenience only - the server decides. */
export const UPLOAD_ACCEPT = Object.keys(FILE_KINDS).join(',')

export function fileKindOf(mimeType: string): FileKind {
  return (FILE_KINDS as Record<string, { kind: FileKind } | undefined>)[mimeType]?.kind ?? 'other'
}

/** "PDF" rather than "application/pdf"; falls back to the raw type. */
export function fileKindLabel(mimeType: string): string {
  return (FILE_KINDS as Record<string, { label: string } | undefined>)[mimeType]?.label ?? mimeType
}

// -------------------------------------------------------------- derived -----

/**
 * Server-side retention ceiling, mirroring `MAX_RETENTION_DAYS` in
 * `apps/api/src/modules/documents/schemas.py`.
 *
 * Checked here only so the dialog can say why a date is refused; the API
 * enforces it regardless.
 */
export const MAX_RETENTION_DAYS = 365

/**
 * Past its retention horizon.
 *
 * Worth computing rather than reading off a status field, because it decides
 * what the row may still do: an expired document lists (with `include_expired`)
 * but its metadata and download routes answer 404, while deleting it remains
 * legal - that is the purge path the state machine declares.
 */
export function isExpired(document: DocumentRow, now: Date = new Date()): boolean {
  return document.expires_at !== null && new Date(document.expires_at) <= now
}

/** Both halves of an attachment reference, or neither. */
export function hasEntityReference(
  document: DocumentRow,
): document is DocumentRow & { entity_type: string; entity_id: string } {
  return Boolean(document.entity_type && document.entity_id)
}

// ---------------------------------------------------------------- hooks -----

/**
 * The document centre: every file this company has stored, and what it is filed
 * against.
 *
 * Documents attach to records - a QC certificate to an inspection, a packing
 * list to a packing order - so the list is filterable by that reference, and a
 * row's attachment is clickable to narrow the list to everything filed against
 * the same record. Category, type, uploader, visibility, the retention window
 * and the search term are all query parameters the API answers; a company's
 * whole file store is never pulled into the browser (master rules 31/36/78).
 *
 * Three behaviours here follow the API rather than intuition:
 *
 * - **Downloads are authenticated requests, not links.** The API never exposes a
 *   filesystem path, so the file arrives as a bearer-authorised response that
 *   `api.download` turns into a temporary object URL. An `<a href>` would arrive
 *   without the token and 401.
 * - **An expired document is listed but not served.** Past `expires_at` the
 *   metadata and download routes answer 404 - the row is only visible at all
 *   because "Include expired" was ticked - so Download is withheld on those
 *   rows while Delete stays available. Deleting an expired file is the purge
 *   path the API's own state machine declares, and it is how a lapsed export is
 *   cleared before the sweep gets to it.
 * - **Deleting takes a reason and cannot be undone.** There is no soft delete
 *   for a stored file: the row and the bytes go together and the audit entry is
 *   what remembers it, which is why the reason is mandatory rather than polite.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ColumnDef } from '@tanstack/react-table'
import {
  Download,
  File as FileIcon,
  FileSpreadsheet,
  FileText,
  Image as ImageIcon,
  Paperclip,
  Trash2,
  Upload,
  X,
  type LucideIcon,
} from 'lucide-react'
import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, DescriptionList, FieldError, PageHeader } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Checkbox,
  Input,
  Label,
} from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { formatDate, formatDateTime, formatFileSize, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import {
  DOCUMENT_CATEGORIES,
  ENTITY_TYPE_PATTERN,
  FILE_KINDS,
  KNOWN_ENTITY_TYPES,
  MAX_RETENTION_DAYS,
  UPLOAD_ACCEPT,
  UUID_PATTERN,
  buildUploadBody,
  documentKeys,
  fileKindLabel,
  fileKindOf,
  hasEntityReference,
  isExpired,
  useUploaderOptions,
  type DocumentCategory,
  type DocumentRow,
  type FileKind,
  type SortableDocumentField,
} from './document-model'

/** Radix Select forbids an empty option value, so "no filter" needs a sentinel. */
const ALL = '__all'

/**
 * Every filter key this screen owns, named once.
 *
 * The Clear button and the count of what is applied read the same list, so a
 * filter added to the toolbar cannot end up counted but not cleared.
 */
const FILTER_KEYS = [
  'category',
  'entity_type',
  'entity_id',
  'mime_type',
  'uploaded_by',
  'visibility',
  'include_expired',
] as const

/**
 * Newest first.
 *
 * Typed against the API's sortable set, so a default the server would answer
 * with a 400 does not compile.
 */
const DEFAULT_SORT: SortableDocumentField = 'created_at'

const KIND_ICON: Record<FileKind, LucideIcon> = {
  pdf: FileText,
  image: ImageIcon,
  sheet: FileSpreadsheet,
  other: FileIcon,
}

/** Storage class, and with it the default retention the server applies. */
const CATEGORY_VARIANT: Record<DocumentCategory, 'default' | 'secondary' | 'muted' | 'warning'> = {
  DOCUMENT: 'default',
  ATTACHMENT: 'secondary',
  REPORT: 'secondary',
  // Generated artefacts: given a retention by the server and swept away after
  // it, so they are visibly not the same kind of thing as an uploaded contract.
  EXPORT: 'warning',
  TEMP: 'muted',
}

const MIME_TYPES = Object.keys(FILE_KINDS)

/** Retention as a sentence, because "null" here means "kept for good". */
function retentionOf(doc: DocumentRow): { label: string; expired: boolean } {
  if (doc.expires_at === null) return { label: 'Kept indefinitely', expired: false }
  return isExpired(doc)
    ? { label: `Expired ${formatDate(doc.expires_at)}`, expired: true }
    : { label: `Until ${formatDate(doc.expires_at)}`, expired: false }
}

// ------------------------------------------------------------------ upload --

interface UploadErrors {
  file?: string
  entity?: string
  expires?: string
}

/**
 * Upload a file and, optionally, file it against a record.
 *
 * Plain state rather than react-hook-form: the field that matters is a `File`,
 * which is not a form value the resolver can carry, and the remaining four
 * fields have one cross-field rule between them.
 *
 * The rules mirrored below are the server's, so a mistake is caught before a
 * 25 MB body is sent - but the server remains the authority: it also checks the
 * declared type against the file's leading bytes, and that check cannot be
 * anticipated here.
 */
function UploadDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const queryClient = useQueryClient()

  const [file, setFile] = useState<File | null>(null)
  const [category, setCategory] = useState<DocumentCategory>('DOCUMENT')
  const [entityType, setEntityType] = useState('')
  const [entityId, setEntityId] = useState('')
  const [isPublic, setIsPublic] = useState(false)
  const [expiresAt, setExpiresAt] = useState('')
  const [errors, setErrors] = useState<UploadErrors>({})

  /**
   * The dialog stays mounted while closed, so every way out empties it - the
   * Cancel button included. Reopening it holding the previous file would be an
   * excellent way to upload the same certificate twice.
   */
  const close = () => {
    setFile(null)
    setCategory('DOCUMENT')
    setEntityType('')
    setEntityId('')
    setIsPublic(false)
    setExpiresAt('')
    setErrors({})
    onOpenChange(false)
  }

  const upload = useMutation({
    mutationFn: (body: FormData) => api.post<DocumentRow>('/documents', body),
    onSuccess: (stored) => {
      void queryClient.invalidateQueries({ queryKey: documentKeys.all })
      toast.success('File uploaded', `${stored.file_name} is stored and can be downloaded.`)
      close()
    },
  })

  const validate = (): UploadErrors => {
    const next: UploadErrors = {}
    if (!file) next.file = 'Choose a file to upload'

    const type = entityType.trim().toLowerCase()
    const id = entityId.trim()
    if (Boolean(type) !== Boolean(id)) {
      // Half a reference files the document against something no query can
      // express, which is why the API refuses it outright.
      next.entity = 'Give both the record type and its id, or neither'
    } else if (type && !ENTITY_TYPE_PATTERN.test(type)) {
      next.entity = 'The record type is lowercase snake_case, e.g. qc_inspection'
    } else if (id && !UUID_PATTERN.test(id)) {
      next.entity = 'The record id is a UUID'
    }

    if (expiresAt) {
      const moment = new Date(expiresAt)
      const days = (moment.getTime() - Date.now()) / 86_400_000
      if (Number.isNaN(moment.getTime())) next.expires = 'Enter a valid date and time'
      else if (days <= 0) next.expires = 'A retention date must be in the future'
      else if (days > MAX_RETENTION_DAYS) {
        next.expires = `At most ${MAX_RETENTION_DAYS} days ahead - leave it empty to keep the file for good`
      }
    }
    return next
  }

  const submit = () => {
    const found = validate()
    setErrors(found)
    if (Object.keys(found).length > 0 || !file) return

    const type = entityType.trim().toLowerCase()
    upload.mutate(
      buildUploadBody({
        file,
        category,
        entity_type: type || null,
        entity_id: type ? entityId.trim() : null,
        is_public: isPublic,
        // `datetime-local` yields a naive "2026-09-10T17:00", which the API
        // rejects for having no offset. `Date` reads it in the browser's zone
        // and `toISOString` states that zone explicitly.
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
      }),
    )
  }

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? onOpenChange(true) : close())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Upload a file</DialogTitle>
          <DialogDescription>
            Stored against this company and downloadable only by someone signed in to it. The server
            checks the content against the type it claims to be.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <div>
            <Label htmlFor="document-file" required>
              File
            </Label>
            <Input
              id="document-file"
              type="file"
              accept={UPLOAD_ACCEPT}
              className="mt-1.5 py-1.5"
              aria-invalid={Boolean(errors.file)}
              onChange={(event) => {
                setFile(event.target.files?.[0] ?? null)
                setErrors((current) => ({ ...current, file: undefined }))
              }}
            />
            <p className="mt-1 text-xs text-muted-foreground">
              {file
                ? `${file.name} - ${formatFileSize(file.size)}`
                : 'PDF, JPEG, PNG, WebP, CSV or Excel. Your administrator may allow fewer.'}
            </p>
            <FieldError message={errors.file} />
          </div>

          <div>
            <Label htmlFor="document-category">Category</Label>
            <Select value={category} onValueChange={(v) => setCategory(v as DocumentCategory)}>
              <SelectTrigger id="document-category" className="mt-1.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {DOCUMENT_CATEGORIES.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {humanise(entry)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="mt-1 text-xs text-muted-foreground">
              Where the file is kept. Exports and temporary files are given a retention
              automatically; an uploaded business document is kept until someone deletes it.
            </p>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label htmlFor="document-entity-type">Attach to record type</Label>
              <Input
                id="document-entity-type"
                list="document-entity-type-options"
                placeholder="qc_inspection"
                autoComplete="off"
                className="mt-1.5"
                value={entityType}
                aria-invalid={Boolean(errors.entity)}
                onChange={(event) => setEntityType(event.target.value)}
              />
              <datalist id="document-entity-type-options">
                {KNOWN_ENTITY_TYPES.map((entry) => (
                  <option key={entry} value={entry} />
                ))}
              </datalist>
            </div>
            <div>
              <Label htmlFor="document-entity-id">Record id</Label>
              <Input
                id="document-entity-id"
                placeholder="UUID of the record"
                autoComplete="off"
                className="mt-1.5 font-mono text-xs"
                value={entityId}
                aria-invalid={Boolean(errors.entity)}
                onChange={(event) => setEntityId(event.target.value)}
              />
            </div>
          </div>
          <p className="-mt-2 text-xs text-muted-foreground">
            Optional, but both halves together: a file filed against half a reference can never be
            found again. Leave both empty for a standalone document.
          </p>
          <FieldError message={errors.entity} />

          <div>
            <Label htmlFor="document-expires">Delete automatically after</Label>
            <Input
              id="document-expires"
              type="datetime-local"
              className="mt-1.5"
              value={expiresAt}
              aria-invalid={Boolean(errors.expires)}
              onChange={(event) => setExpiresAt(event.target.value)}
            />
            <p className="mt-1 text-xs text-muted-foreground">
              Leave empty to keep it indefinitely. At most {MAX_RETENTION_DAYS} days ahead.
            </p>
            <FieldError message={errors.expires} />
          </div>

          <div className="flex items-start gap-2.5 rounded-md border p-3">
            <Checkbox
              id="document-public"
              className="mt-0.5"
              checked={isPublic}
              onCheckedChange={(checked) => setIsPublic(checked === true)}
            />
            <div>
              <Label htmlFor="document-public" className="cursor-pointer">
                Not confidential within the company
              </Label>
              <p className="mt-1 text-xs text-muted-foreground">
                A classification for colleagues, not a relaxation: every download still needs a
                signed-in account in this company.
              </p>
            </div>
          </div>

          {upload.error && (
            <Alert variant="destructive">
              <AlertTitle>The file was not stored</AlertTitle>
              <AlertDescription>{upload.error.message}</AlertDescription>
            </Alert>
          )}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={close} disabled={upload.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} loading={upload.isPending}>
            Upload
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------------ detail --

/**
 * One document's full metadata.
 *
 * The list row and `GET /documents/{id}` are the same schema, so the dialog
 * renders from the row it was opened on and uses the fetch to confirm the file
 * is still there - a document deleted or expired in another tab comes back 404
 * and is said so plainly, rather than offering a download that cannot work.
 *
 * The checksum is shown because it is the only thing that can prove a restored
 * copy is the file that was uploaded.
 */
function DocumentDetailDialog({
  doc,
  canView,
  canDelete,
  downloading,
  onDownload,
  onDelete,
  onOpenChange,
}: {
  doc: DocumentRow | null
  canView: boolean
  canDelete: boolean
  downloading: boolean
  onDownload: (doc: DocumentRow) => void
  onDelete: (doc: DocumentRow) => void
  onOpenChange: (open: boolean) => void
}) {
  const expired = doc ? isExpired(doc) : false

  const detail = useQuery({
    queryKey: documentKeys.detail(doc?.id ?? ''),
    queryFn: () => api.get<DocumentRow>(`/documents/${doc?.id}`),
    // An expired document 404s on this route by design, and the row in hand
    // already carries everything the route would return.
    enabled: Boolean(doc) && !expired && canView,
    staleTime: staleTimes.transactional,
  })

  const shown = detail.data ?? doc
  const retention = shown ? retentionOf(shown) : null

  return (
    <Dialog open={Boolean(doc)} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle className="break-all">{shown?.file_name ?? 'Document'}</DialogTitle>
          <DialogDescription>
            {shown ? `${fileKindLabel(shown.mime_type)} - ${formatFileSize(shown.size_bytes)}` : ''}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {/* No skeleton: the row this was opened on is already the whole
              schema, so the fetch confirms rather than fills. */}
          {detail.isFetching && (
            <p className="text-xs text-muted-foreground" role="status">
              Checking the file is still there...
            </p>
          )}

          {expired && (
            <Alert variant="warning">
              <AlertTitle>Past its retention date</AlertTitle>
              <AlertDescription>
                The API treats an expired document as absent, so the file can no longer be
                downloaded. Deleting it still works and clears the record and the file together.
              </AlertDescription>
            </Alert>
          )}

          {!expired && detail.error && (
            <Alert variant="destructive">
              <AlertTitle>This document is no longer available</AlertTitle>
              <AlertDescription>{detail.error.message}</AlertDescription>
            </Alert>
          )}

          {shown && (
            <DescriptionList
              columns={2}
              items={[
                { label: 'Category', value: humanise(shown.category) },
                { label: 'Content type', value: shown.mime_type },
                { label: 'Size', value: formatFileSize(shown.size_bytes) },
                {
                  label: 'Attached to',
                  value: hasEntityReference(shown) ? (
                    <span>
                      {humanise(shown.entity_type)}
                      <span className="mt-0.5 block break-all font-mono text-2xs text-muted-foreground">
                        {shown.entity_id}
                      </span>
                    </span>
                  ) : (
                    <span className="text-muted-foreground">
                      Standalone - filed against nothing
                    </span>
                  ),
                },
                {
                  label: 'Visibility',
                  value: shown.is_public
                    ? 'Open to everyone in the company'
                    : 'Restricted to those who can view documents',
                },
                { label: 'Retention', value: retention?.label },
                { label: 'Uploaded', value: formatDateTime(shown.created_at) },
                {
                  label: 'Checksum (SHA-256)',
                  value: (
                    <span className="break-all font-mono text-2xs">{shown.checksum_sha256}</span>
                  ),
                },
                {
                  label: 'Document id',
                  value: <span className="break-all font-mono text-2xs">{shown.id}</span>,
                },
              ]}
            />
          )}
        </DialogBody>

        <DialogFooter>
          {canDelete && shown && (
            <Button variant="destructive" onClick={() => onDelete(shown)}>
              <Trash2 />
              Delete
            </Button>
          )}
          {canView && (
            <Button
              variant="outline"
              disabled={expired || !shown}
              loading={downloading}
              onClick={() => shown && onDownload(shown)}
            >
              <Download />
              Download
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// -------------------------------------------------------------------- page --

export default function DocumentsPage() {
  const { can, user } = useAuth()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const [detailDoc, setDetailDoc] = useState<DocumentRow | null>(null)
  const [pendingDelete, setPendingDelete] = useState<DocumentRow | null>(null)
  const [reason, setReason] = useState('')
  const [reasonTouched, setReasonTouched] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)

  // One right covers all three reads - the list, the metadata and the download -
  // so it is asserted here as well as on the route, and no read affordance is
  // offered on the strength of the route gate alone.
  const canView = can(P.DOCUMENT_VIEW)
  const canUpload = can(P.DOCUMENT_UPLOAD)
  const canDelete = can(P.DOCUMENT_DELETE)
  // `GET /users` is what names an uploader, and it enforces its own right.
  // Without it the list is unaffected - uploaders simply stay anonymous and the
  // filter narrows to the signed-in user instead of naming colleagues.
  const canViewUsers = can(P.USER_VIEW)

  const category = searchParams.get('category') ?? ''
  const entityType = searchParams.get('entity_type') ?? ''
  const entityId = searchParams.get('entity_id') ?? ''
  const mimeType = searchParams.get('mime_type') ?? ''
  const uploadedBy = searchParams.get('uploaded_by') ?? ''
  const visibility = searchParams.get('visibility') ?? ''
  const includeExpired = searchParams.get('include_expired') === 'true'

  const list = useListQuery<DocumentRow>({
    path: '/documents',
    queryKey: documentKeys.list,
    defaultSortBy: DEFAULT_SORT,
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
    filters: {
      ...(category ? { category } : {}),
      ...(entityType ? { entity_type: entityType } : {}),
      // `entity_id` is legal only beside `entity_type`, and only as a UUID - the
      // API 422s on either mistake. A half-typed id therefore does not narrow
      // the list yet rather than breaking it.
      ...(entityType && UUID_PATTERN.test(entityId) ? { entity_id: entityId } : {}),
      ...(mimeType ? { mime_type: mimeType } : {}),
      ...(uploadedBy ? { uploaded_by_id: uploadedBy } : {}),
      ...(visibility === 'public' ? { is_public: true } : {}),
      ...(visibility === 'restricted' ? { is_public: false } : {}),
      // Off by default on the server too: a lapsed export drops out of every
      // ordinary listing without waiting for the purge sweep.
      ...(includeExpired ? { include_expired: true } : {}),
    },
  })

  const { setParam } = list
  const filterCount = FILTER_KEYS.filter((key) => Boolean(searchParams.get(key))).length

  const uploaders = useUploaderOptions(canViewUsers)
  const uploaderNames = useMemo(
    () => new Map((uploaders.data ?? []).map((entry) => [entry.id, entry.full_name] as const)),
    [uploaders.data],
  )

  /**
   * Clear every filter in ONE URL write.
   *
   * `setSearchParams` navigates from the `searchParams` captured when the
   * callback was made, so a `setParam` per key would leave all but the last
   * change behind.
   */
  const clearFilters = useCallback(() => {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        for (const key of FILTER_KEYS) next.delete(key)
        next.delete('page')
        return next
      },
      { replace: true },
    )
  }, [setSearchParams])

  /** Narrow the list to everything filed against the same record. */
  const showAttachmentsOf = useCallback(
    (type: string, id: string) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current)
          next.set('entity_type', type)
          next.set('entity_id', id)
          next.delete('page')
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const download = useMutation({
    mutationFn: (doc: DocumentRow) => api.download(`/documents/${doc.id}/download`, doc.file_name),
  })
  // `mutate` is stable across renders; the mutation object is not, so the column
  // memo holds the function rather than the result of `useMutation`.
  const startDownload = download.mutate
  const downloadingId = download.isPending ? (download.variables?.id ?? null) : null

  const remove = useMutation({
    // The reason travels in the query string, not a body: a DELETE body is legal
    // but proxies drop it, and the audit entry is the only record left of a file
    // that no longer exists.
    mutationFn: ({ doc, reason: text }: { doc: DocumentRow; reason: string }) =>
      api.delete(`/documents/${doc.id}`, { params: { reason: text } }),
    onSuccess: (_result, variables) => {
      void queryClient.invalidateQueries({ queryKey: documentKeys.all })
      toast.success('Document deleted', `${variables.doc.file_name} and its file are gone.`)
      setPendingDelete(null)
      setDetailDoc(null)
      setReason('')
      setReasonTouched(false)
    },
  })

  /**
   * An uploader id as a person.
   *
   * The lookup covers one page of active users, so an id it does not hold - a
   * deactivated colleague, a very large company, an account without the right
   * to read users at all - is expected rather than an error, and says so
   * instead of rendering a raw UUID.
   */
  const uploaderName = useCallback(
    (id: string | null): ReactNode => {
      if (id === null) return <span className="text-muted-foreground">Not recorded</span>
      if (id === user?.id) return 'You'
      const name = uploaderNames.get(id)
      if (name) return name
      return (
        <span className="text-muted-foreground" title={id}>
          Another user
        </span>
      )
    },
    [uploaderNames, user?.id],
  )

  const columns = useMemo<ColumnDef<DocumentRow, unknown>[]>(() => {
    const defs: ColumnDef<DocumentRow, unknown>[] = [
      {
        accessorKey: 'file_name',
        header: 'File',
        cell: ({ row }) => {
          const Icon = KIND_ICON[fileKindOf(row.original.mime_type)]
          return (
            <div className="flex items-center gap-2">
              <Icon className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
              <button
                type="button"
                // The row itself opens the detail; the controls inside it stop
                // the click travelling so they do not fire twice over.
                onClick={(event) => {
                  event.stopPropagation()
                  setDetailDoc(row.original)
                }}
                aria-label={`Show the details of ${row.original.file_name}`}
                className="min-w-0 rounded-sm text-left hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <span className="block max-w-[22rem] truncate font-medium">
                  {row.original.file_name}
                </span>
                <span className="block text-2xs text-muted-foreground">
                  {fileKindLabel(row.original.mime_type)}
                </span>
              </button>
            </div>
          )
        },
      },
      {
        accessorKey: 'category',
        header: 'Category',
        cell: ({ row }) => (
          <Badge variant={CATEGORY_VARIANT[row.original.category]}>
            {humanise(row.original.category)}
          </Badge>
        ),
      },
      {
        id: 'entity',
        header: 'Attached to',
        // Not sortable: the API sorts on file name, size, category and the two
        // dates only, and anything else comes back a 400.
        enableSorting: false,
        cell: ({ row }) => {
          // Destructured rather than read through `row.original` inside the
          // handler: a property access does not stay narrowed inside a closure,
          // and the pair is only ever both-or-neither.
          const { entity_type: type, entity_id: id } = row.original
          if (!type || !id) return <span className="text-xs text-muted-foreground">Standalone</span>

          return (
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation()
                showAttachmentsOf(type, id)
              }}
              aria-label={`Show every file attached to this ${humanise(type)}`}
              className="rounded-sm text-left hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <span className="flex items-center gap-1.5 text-xs">
                <Paperclip className="h-3 w-3 text-muted-foreground" aria-hidden />
                {humanise(type)}
              </span>
              <span className="block font-mono text-2xs text-muted-foreground">
                {id.slice(0, 8)}
              </span>
            </button>
          )
        },
      },
      {
        accessorKey: 'size_bytes',
        header: 'Size',
        meta: { align: 'right' },
        cell: ({ row }) => (
          <span className="tabular">{formatFileSize(row.original.size_bytes)}</span>
        ),
      },
      {
        id: 'is_public',
        header: 'Visibility',
        enableSorting: false,
        // "Public" never means reachable without signing in - it marks content
        // that is not confidential within the company.
        cell: ({ row }) =>
          row.original.is_public ? (
            <Badge variant="secondary">Company-wide</Badge>
          ) : (
            <Badge variant="muted">Restricted</Badge>
          ),
      },
      {
        accessorKey: 'expires_at',
        header: 'Retention',
        cell: ({ row }) => {
          const retention = retentionOf(row.original)
          return retention.expired ? (
            <Badge variant="warning">{retention.label}</Badge>
          ) : (
            <span className="text-xs text-muted-foreground">{retention.label}</span>
          )
        },
      },
      {
        accessorKey: 'created_at',
        header: 'Uploaded',
        cell: ({ row }) => <span className="text-xs">{formatDate(row.original.created_at)}</span>,
      },
      {
        id: 'uploaded_by_id',
        header: 'Uploaded by',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-xs">{uploaderName(row.original.uploaded_by_id)}</span>
        ),
      },
      {
        id: 'actions',
        header: '',
        enableSorting: false,
        enableHiding: false,
        meta: { align: 'right' },
        cell: ({ row }) => {
          const expired = isExpired(row.original)
          return (
            <div className="flex items-center justify-end gap-1">
              {canView && (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Download ${row.original.file_name}`}
                  // An expired document is served as 404, so the button would
                  // only ever produce an error toast.
                  disabled={expired}
                  title={
                    expired ? 'Past its retention date - the file is no longer served' : undefined
                  }
                  loading={downloadingId === row.original.id}
                  onClick={(event) => {
                    event.stopPropagation()
                    startDownload(row.original)
                  }}
                >
                  <Download />
                </Button>
              )}
              {canDelete && (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Delete ${row.original.file_name}`}
                  onClick={(event) => {
                    event.stopPropagation()
                    setPendingDelete(row.original)
                  }}
                >
                  <Trash2 />
                </Button>
              )}
            </div>
          )
        },
      },
    ]

    return defs
  }, [canDelete, canView, downloadingId, showAttachmentsOf, startDownload, uploaderName])

  const uploadButton = canUpload ? (
    <Button onClick={() => setUploadOpen(true)}>
      <Upload />
      Upload a file
    </Button>
  ) : null

  const narrowed = filterCount > 0 || list.search !== ''
  const reasonTooShort = reason.trim().length < 3

  return (
    <>
      <PageHeader
        title="Documents"
        description="Every file stored against this company: certificates and drawings uploaded by hand, packing lists and reports generated for you. Downloads are authenticated - a file is never reachable by URL alone."
        actions={uploadButton}
      />

      <DataTable
        columns={columns}
        data={list.data}
        pagination={list.pagination}
        isLoading={list.isLoading}
        isFetching={list.isFetching}
        error={list.error}
        onPageChange={list.setPage}
        onLimitChange={list.setLimit}
        onSortChange={list.setSort}
        onSearchChange={list.setSearch}
        onRefresh={() => void list.refetch()}
        searchValue={list.search}
        // The API searches the original file name and nothing else: the storage
        // key is server-generated noise nobody would search for.
        searchPlaceholder="Search file name..."
        getRowId={(row) => row.id}
        onRowClick={(row) => setDetailDoc(row)}
        emptyTitle="No documents found"
        emptyDescription={
          narrowed
            ? 'Nothing matches these filters. Clear one, or tick "Include expired" to see files past their retention date.'
            : canUpload
              ? 'Files attached to an inspection, a packing order or a report land here. Upload one to start.'
              : 'Files attached to an inspection, a packing order or a report land here. Nothing has been stored yet.'
        }
        emptyAction={narrowed ? undefined : uploadButton}
        toolbar={
          <>
            <Select
              value={category || ALL}
              onValueChange={(next) => setParam('category', next === ALL ? undefined : next)}
            >
              <SelectTrigger className="w-[10.5rem]" aria-label="Filter by category">
                <SelectValue placeholder="Any category" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any category</SelectItem>
                {DOCUMENT_CATEGORIES.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {humanise(entry)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select
              value={mimeType || ALL}
              onValueChange={(next) => setParam('mime_type', next === ALL ? undefined : next)}
            >
              <SelectTrigger className="w-[10.5rem]" aria-label="Filter by file type">
                <SelectValue placeholder="Any file type" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any file type</SelectItem>
                {MIME_TYPES.map((entry) => (
                  <SelectItem key={entry} value={entry}>
                    {fileKindLabel(entry)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <div className="flex items-center gap-1.5">
              <Label htmlFor="documents-entity-type" className="sr-only">
                Attached to record type
              </Label>
              <Input
                id="documents-entity-type"
                list="documents-entity-type-options"
                className="w-[12rem]"
                placeholder="Attached to, e.g. qc_inspection"
                autoComplete="off"
                value={entityType}
                onChange={(event) => setParam('entity_type', event.target.value || undefined)}
              />
              <datalist id="documents-entity-type-options">
                {KNOWN_ENTITY_TYPES.map((entry) => (
                  <option key={entry} value={entry} />
                ))}
              </datalist>

              <Label htmlFor="documents-entity-id" className="sr-only">
                Attached to record id
              </Label>
              <Input
                id="documents-entity-id"
                className="w-[13rem] font-mono text-xs"
                placeholder="Record id (UUID)"
                autoComplete="off"
                value={entityId}
                // Meaningless without the type beside it, and the API says so
                // with a 422 rather than a wide scan.
                disabled={!entityType}
                // Flagged rather than sent: a partial id narrows nothing, and
                // the field should say so instead of quietly listing everything
                // of that record type.
                aria-invalid={entityId !== '' && !UUID_PATTERN.test(entityId)}
                onChange={(event) => setParam('entity_id', event.target.value || undefined)}
              />
            </div>

            {canViewUsers ? (
              <Select
                value={uploadedBy || ALL}
                onValueChange={(next) => setParam('uploaded_by', next === ALL ? undefined : next)}
              >
                <SelectTrigger className="w-[12rem]" aria-label="Filter by uploader">
                  <SelectValue placeholder="Anyone" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>Uploaded by anyone</SelectItem>
                  {(uploaders.data ?? []).map((entry) => (
                    <SelectItem key={entry.id} value={entry.id}>
                      {entry.full_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              // Naming colleagues needs the user register; narrowing to your own
              // uploads needs only the id already in the session.
              <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3">
                <Checkbox
                  id="documents-mine"
                  checked={Boolean(user && uploadedBy === user.id)}
                  onCheckedChange={(checked) =>
                    setParam('uploaded_by', checked === true ? (user?.id ?? undefined) : undefined)
                  }
                />
                <Label htmlFor="documents-mine" className="cursor-pointer text-xs font-normal">
                  Only my uploads
                </Label>
              </div>
            )}

            <Select
              value={visibility || ALL}
              onValueChange={(next) => setParam('visibility', next === ALL ? undefined : next)}
            >
              <SelectTrigger className="w-[11rem]" aria-label="Filter by visibility">
                <SelectValue placeholder="Any visibility" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any visibility</SelectItem>
                <SelectItem value="public">Company-wide</SelectItem>
                <SelectItem value="restricted">Restricted</SelectItem>
              </SelectContent>
            </Select>

            <div className="flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3">
              <Checkbox
                id="documents-include-expired"
                checked={includeExpired}
                onCheckedChange={(checked) =>
                  setParam('include_expired', checked === true ? 'true' : undefined)
                }
              />
              <Label
                htmlFor="documents-include-expired"
                className="cursor-pointer text-xs font-normal"
              >
                Include expired
              </Label>
            </div>

            {filterCount > 0 && (
              <Button variant="ghost" size="sm" onClick={clearFilters}>
                <X />
                Clear {filterCount}
              </Button>
            )}
          </>
        }
      />

      <DocumentDetailDialog
        doc={detailDoc}
        canView={canView}
        canDelete={canDelete}
        downloading={downloadingId !== null && downloadingId === detailDoc?.id}
        onDownload={(doc) => startDownload(doc)}
        onDelete={(doc) => {
          // One dialog at a time. The confirmation names the file, so closing
          // the detail behind it loses no context and avoids two focus traps
          // fighting over the same keyboard.
          setDetailDoc(null)
          setPendingDelete(doc)
        }}
        onOpenChange={(open) => !open && setDetailDoc(null)}
      />

      {canUpload && <UploadDialog open={uploadOpen} onOpenChange={setUploadOpen} />}

      <ConfirmDialog
        open={Boolean(pendingDelete)}
        onOpenChange={(open) => {
          if (!open) {
            setPendingDelete(null)
            setReason('')
            setReasonTouched(false)
          }
        }}
        title="Delete this document?"
        description={
          pendingDelete
            ? `${pendingDelete.file_name} and the stored file are removed together, permanently. The audit trail keeps your reason as the only record that it existed.`
            : undefined
        }
        confirmLabel="Delete document"
        destructive
        loading={remove.isPending}
        onConfirm={() => {
          if (!pendingDelete) return
          if (reasonTooShort) {
            setReasonTouched(true)
            return
          }
          remove.mutate({ doc: pendingDelete, reason: reason.trim() })
        }}
      >
        <div>
          <Label htmlFor="document-delete-reason" required>
            Reason
          </Label>
          <Input
            id="document-delete-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            autoComplete="off"
            // The API accepts 3 to 255 characters and rejects anything shorter.
            maxLength={255}
            className="mt-1.5"
            aria-invalid={reasonTouched && reasonTooShort}
          />
          {reasonTouched && reasonTooShort && (
            <FieldError message="Record at least three characters of reason" />
          )}
        </div>
      </ConfirmDialog>
    </>
  )
}

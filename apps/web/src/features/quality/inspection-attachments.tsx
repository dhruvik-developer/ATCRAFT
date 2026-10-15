/**
 * Inspection photos and attachments.
 *
 * Readings carry `photo_document_id`s and the inspection itself carries
 * documents filed with `entity_type='qc_inspection'` - no link table, because
 * the documents module files against any snake_case noun by `(entity_type,
 * entity_id)` and a QC photo outlives nothing it needs a foreign key for.
 *
 * Bytes are never addressed by URL: every thumbnail and every download goes
 * through `GET /documents/{id}/download` as an authenticated request, which is
 * why a thumbnail is a blob object URL fetched here rather than an `<img
 * src>`.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, FileText, Upload } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/primitives'
import { UPLOAD_ACCEPT, fileKindLabel, type DocumentRow } from '@/features/documents/document-model'
import { api } from '@/lib/api-client'
import { formatDateTime } from '@/lib/format'
import { success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { staleTimes } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import { useAuth } from '@/features/auth/auth-context'

const ATTACHMENT_LIST_KEY = (inspectionId: string) =>
  ['quality', 'attachments', inspectionId] as const

/**
 * Best-effort object-URL release.
 *
 * Revocation is a memory hint, not correctness: a missed one costs a blob
 * until the tab closes. Guarded rather than assumed, because the call runs in
 * effect cleanup - including in test environments without the function, where
 * an unguarded call would fail the unmount rather than the behaviour.
 */
function releaseObjectUrl(url: string | null) {
  if (url && typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(url)
}

function useAttachmentList(inspectionId: string, enabled: boolean) {
  const { can } = useAuth()
  return useQuery({
    queryKey: ATTACHMENT_LIST_KEY(inspectionId),
    queryFn: () =>
      api.getPage<DocumentRow>('/documents', {
        params: {
          entity_type: 'qc_inspection',
          entity_id: inspectionId,
          sort_by: 'created_at',
          sort_dir: 'desc',
          limit: 50,
          with_total: false,
        },
      }),
    enabled: enabled && Boolean(inspectionId) && can(P.DOCUMENT_VIEW),
    staleTime: staleTimes.transactional,
  })
}

/**
 * One photo thumbnail.
 *
 * The blob's own content type decides between an image and a file chip - the
 * reading carries only the document id, and a second metadata round trip per
 * photo would be an N+1 the list endpoint already answers in one page.
 */
/**
 * One photo thumbnail.
 *
 * The reading carries only the document id, so the kind comes from the
 * document's own metadata - one small cached read per photo, bounded by the
 * template's parameter count. Only images are fetched as blobs; anything else
 * offers the download straight away.
 */
export function PhotoThumb({ documentId, label }: { documentId: string; label: string }) {
  const { can } = useAuth()
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  const meta = useQuery({
    queryKey: ['quality', 'photo-meta', documentId] as const,
    queryFn: () => api.get<DocumentRow>(`/documents/${documentId}`),
    enabled: Boolean(documentId) && can(P.DOCUMENT_VIEW),
    staleTime: staleTimes.transactional,
  })
  const isImage = meta.data?.mime_type?.startsWith('image/') ?? false

  useEffect(() => {
    if (!isImage) return
    let live = true
    let objectUrl: string | null = null
    setUrl(null)
    setFailed(false)
    // One authenticated fetch; the same bytes back the <img>.
    void api
      .blobUrl(`/documents/${documentId}/download`)
      .then((created) => {
        if (!live) {
          releaseObjectUrl(created)
          return
        }
        objectUrl = created
        setUrl(created)
      })
      .catch(() => {
        // Bytes gone while the row survives: offer the download, which 404s
        // plainly, rather than a thumbnail that never resolves.
        if (live) setFailed(true)
      })
    return () => {
      live = false
      releaseObjectUrl(objectUrl)
    }
  }, [documentId, isImage])

  const download = (
    <Button
      variant="outline"
      size="sm"
      onClick={() => void api.download(`/documents/${documentId}/download`, label)}
    >
      <Download />
      {label}
    </Button>
  )

  if (!isImage || failed) return download
  if (url === null) return <Skeleton className="h-16 w-16 rounded" />
  return (
    <button
      type="button"
      className="block overflow-hidden rounded border"
      onClick={() => void api.download(`/documents/${documentId}/download`, label)}
      title={`Download ${label}`}
    >
      <img src={url} alt={label} className="h-16 w-16 object-cover" loading="lazy" />
    </button>
  )
}

function AttachmentRow({ document }: { document: DocumentRow }) {
  const isImage = document.mime_type.startsWith('image/')
  return (
    <li className="flex items-center gap-3 rounded border px-3 py-2">
      {isImage ? (
        <PhotoThumb documentId={document.id} label={document.file_name} />
      ) : (
        <FileText className="h-8 w-8 shrink-0 text-muted-foreground" aria-hidden />
      )}
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium">{document.file_name}</p>
        <p className="text-xs text-muted-foreground">
          {fileKindLabel(document.mime_type)} - {formatDateTime(document.created_at)}
        </p>
      </div>
      <Button
        variant="outline"
        size="sm"
        onClick={() => void api.download(`/documents/${document.id}/download`, document.file_name)}
      >
        <Download />
        Get
      </Button>
    </li>
  )
}

/**
 * Files attached to the inspection itself, including inspections raised
 * without a template - the gap P11.F3 names. Uploads file under the
 * inspection's own entity reference; reading photos stay on the parameter
 * they evidence.
 */
export function InspectionAttachments({ inspectionId }: { inspectionId: string }) {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const fileInput = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)

  const canView = can(P.DOCUMENT_VIEW)
  const canUpload = can(P.DOCUMENT_UPLOAD)

  const list = useAttachmentList(inspectionId, true)
  const rows = list.data?.data ?? []

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const body = new FormData()
      body.append('file', file, file.name)
      body.append('entity_type', 'qc_inspection')
      body.append('entity_id', inspectionId)
      body.append('category', 'ATTACHMENT')
      return api.post<DocumentRow>('/documents', body)
    },
    onSuccess: (document) => {
      void queryClient.invalidateQueries({ queryKey: ATTACHMENT_LIST_KEY(inspectionId) })
      toast.success(success.created('Attachment'), `${document.file_name} attached.`)
    },
    onSettled: () => setUploading(false),
  })

  if (!canView && !canUpload) return null

  return (
    <section aria-label="Attachments">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-medium">Attachments</h3>
        {canUpload && (
          <>
            <input
              ref={fileInput}
              type="file"
              accept={UPLOAD_ACCEPT}
              className="hidden"
              aria-label="Attach a file"
              onChange={(event) => {
                const file = event.target.files?.[0]
                event.target.value = ''
                if (!file) return
                setUploading(true)
                upload.mutate(file)
              }}
            />
            <Button
              variant="outline"
              size="sm"
              disabled={uploading}
              onClick={() => fileInput.current?.click()}
            >
              <Upload />
              {uploading ? 'Attaching…' : 'Attach a file'}
            </Button>
          </>
        )}
      </div>

      {list.isLoading ? (
        <Skeleton className="h-14 w-full" />
      ) : rows.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No files attached yet. Defect photos taken during measurement stay on their parameter
          above; anything else about this lot goes here.
        </p>
      ) : (
        <ul className="space-y-2">
          {rows.map((document) => (
            <AttachmentRow key={document.id} document={document} />
          ))}
        </ul>
      )}
      {upload.error && <p className="mt-2 text-xs text-destructive">{upload.error.message}</p>}
    </section>
  )
}

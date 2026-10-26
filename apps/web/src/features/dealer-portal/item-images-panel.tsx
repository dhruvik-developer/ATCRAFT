import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { EmptyState, FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Badge, Card, CardContent, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { fieldErrorSummary } from '@/lib/form-errors'
import { describeError, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import { DealerImage, DealerPagination, DealerResult } from './dealer-shared'

const IMAGE_TYPES = [
  { mime: 'image/jpeg', label: 'JPEG' },
  { mime: 'image/png', label: 'PNG' },
  { mime: 'image/webp', label: 'WebP' },
] as const

export function ItemImagesPanel({
  itemId,
  primaryImageId,
}: {
  itemId: string
  primaryImageId?: string | null
}) {
  const { can } = useAuth()
  const client = useQueryClient()
  const [params] = useSearchParams()
  const [error, setError] = useState<string>()
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const selectedMime = params.get('images_image_mime')
  const mime = IMAGE_TYPES.find((type) => type.mime === selectedMime)?.mime || 'image/jpeg'
  const list = useListQuery<SchemaOf<'DocumentRead'>>({
    path: '/documents',
    queryKey: (filters) => queryKeys.dealerPortal.list('item-images', filters),
    namespace: 'images',
    defaultLimit: 12,
    filters: { entity_type: 'item', entity_id: itemId, mime_type: mime },
    enabled: can(P.DOCUMENT_VIEW),
  })
  const refresh = () => {
    void client.invalidateQueries({ queryKey: queryKeys.items.detail(itemId) })
    void client.invalidateQueries({ queryKey: queryKeys.dealerPortal.all })
  }
  const primary = useMutation({
    mutationFn: (documentId: string | null) =>
      api.put(`/products/items/${itemId}/image`, { document_id: documentId }),
    onSuccess: () => {
      refresh()
      toast.success(success.updated('Primary image'))
    },
    onError: (failure) => setError(fieldErrorSummary(failure) || describeError(failure)),
  })
  const upload = useMutation({
    mutationFn: async (file: File) => {
      const body = new FormData()
      body.set('file', file)
      body.set('category', 'ATTACHMENT')
      body.set('entity_type', 'item')
      body.set('entity_id', itemId)
      const document = await api.post<SchemaOf<'DocumentRead'>>('/documents', body)
      await api.put(`/products/items/${itemId}/image`, { document_id: document.id })
      return document
    },
    onSuccess: (document) => {
      refresh()
      list.setParam('image_mime', document.mime_type)
      toast.success(success.updated('Item image'))
    },
    onError: (failure) => setError(fieldErrorSummary(failure) || describeError(failure)),
  })
  return (
    <div className="space-y-5">
      {primaryImageId && (
        <section className="max-w-sm space-y-3">
          <h2 className="font-semibold">Primary catalog image</h2>
          <DealerImage
            path={can(P.DOCUMENT_VIEW) ? `/documents/${primaryImageId}/download` : null}
            label="Primary item image"
          />
          {can(P.ITEM_UPDATE) && (
            <Button
              variant="outline"
              onClick={() => primary.mutate(null)}
              disabled={primary.isPending}
            >
              Remove primary image
            </Button>
          )}
        </section>
      )}
      {can(P.ITEM_UPDATE) && can(P.DOCUMENT_UPLOAD) && (
        <form
          className="space-y-3 rounded-lg border p-4"
          onSubmit={(event) => {
            event.preventDefault()
            setError(undefined)
            const selected = selectedFile
            if (!(selected instanceof File) || !selected.size) {
              setError('Choose an image to upload.')
              return
            }
            if (!IMAGE_TYPES.some((type) => type.mime === selected.type)) {
              setError('Choose a JPEG, PNG or WebP image.')
              return
            }
            upload.mutate(selected)
          }}
        >
          <div>
            <Label htmlFor="item-primary-image">Upload image</Label>
            <Input
              id="item-primary-image"
              name="image"
              type="file"
              accept="image/jpeg,image/png,image/webp"
              onChange={(event) => setSelectedFile(event.target.files?.[0] ?? null)}
              className="mt-1"
            />
          </div>
          <p className="text-xs text-muted-foreground">
            The new image becomes primary. Existing attachments remain available in the gallery.
          </p>
          <Button type="submit" loading={upload.isPending}>
            Upload and set primary
          </Button>
        </form>
      )}
      <FieldError message={error} />
      {can(P.DOCUMENT_VIEW) ? (
        <>
          <div>
            <Label htmlFor="item-image-type">Image type</Label>
            <select
              id="item-image-type"
              value={mime}
              onChange={(event) => list.setParam('image_mime', event.target.value)}
              className="mt-1 min-h-11 w-full rounded-md border bg-background px-3 text-sm sm:w-48"
            >
              {IMAGE_TYPES.map((type) => (
                <option key={type.mime} value={type.mime}>
                  {type.label}
                </option>
              ))}
            </select>
          </div>
          <DealerResult loading={list.isLoading} error={list.error}>
            {list.data.length ? (
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {list.data.map((document) => (
                  <Card key={document.id}>
                    <CardContent className="space-y-3 p-4">
                      <DealerImage
                        path={`/documents/${document.id}/download`}
                        label={document.file_name}
                      />
                      <p className="break-words text-sm">{document.file_name}</p>
                      {document.id === primaryImageId ? (
                        <Badge variant="success">Primary</Badge>
                      ) : (
                        can(P.ITEM_UPDATE) && (
                          <Button
                            variant="outline"
                            onClick={() => primary.mutate(document.id)}
                            disabled={primary.isPending}
                          >
                            Set as primary
                          </Button>
                        )
                      )}
                    </CardContent>
                  </Card>
                ))}
              </div>
            ) : (
              <EmptyState
                title="No images of this type"
                description="Choose another image type or upload a new image."
              />
            )}
            <DealerPagination pagination={list.pagination} page={list.page} onPage={list.setPage} />
          </DealerResult>
        </>
      ) : (
        <EmptyState
          title="Image gallery unavailable"
          description="File viewing access is required to browse the gallery."
        />
      )}
    </div>
  )
}

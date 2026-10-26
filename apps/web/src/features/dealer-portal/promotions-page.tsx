import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataTable } from '@/components/data-table'
import { ConfirmDialog, FieldError, PageHeader, StatusBadge } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui/overlays'
import { Input, Label, Textarea } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { CustomerPicker } from '@/features/customers/customer-picker'
import { ItemPicker } from '@/features/inventory/pickers'
import { useCategoryOptions } from '@/features/items/item-model'
import { useBrandOptions } from '@/hooks/use-brand-options'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { PICKER_PAGE_SIZE, REASON_MAX_LENGTH, REASON_MIN_LENGTH } from '@/lib/constants'
import { fieldErrorSummary } from '@/lib/form-errors'
import { formatDate, formatQuantity, humanise } from '@/lib/format'
import { describeError, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

import { DealerImage } from './dealer-shared'
import { bannerPayload, offerPayload } from './promotion-payloads'

type Kind = 'banners' | 'offers'
type Promotion = SchemaOf<'DealerBannerRead'> | SchemaOf<'DealerOfferRead'>
type Lifecycle = 'activate' | 'deactivate' | 'retire'

function OfferPreview({ id }: { id: string }) {
  const [open, setOpen] = useState(false)
  const preview = useQuery({
    queryKey: ['promotions', 'offer-preview', id],
    queryFn: ({ signal }) =>
      api.get<SchemaOf<'DealerOfferPreview'>>(`/promotions/offers/${id}/preview`, { signal }),
    enabled: open,
  })
  return (
    <div>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        Preview scope
      </Button>
      {open && (
        <p className="mt-1 text-sm text-muted-foreground">
          {preview.isLoading
            ? 'Checking products…'
            : preview.error
              ? describeError(preview.error)
              : `Applies to ${preview.data?.item_count ?? 0} products`}
        </p>
      )}
    </div>
  )
}

function PromotionTarget({
  kind,
  value,
  onChange,
}: {
  kind: string
  value: string | null
  onChange: (id: string | null) => void
}) {
  const { can } = useAuth()
  const categories = useCategoryOptions(kind === 'CATEGORY' && can(P.CATEGORY_VIEW))
  const brands = useBrandOptions(kind === 'BRAND' && can(P.BRAND_VIEW))
  const [search, setSearch] = useState('')
  const term = useDebouncedValue(search)
  const offers = useQuery({
    queryKey: ['promotions', 'offer-options', term],
    queryFn: ({ signal }) =>
      api.getPage<SchemaOf<'DealerOfferRead'>>('/promotions/offers', {
        params: { limit: PICKER_PAGE_SIZE, with_total: false, search: term },
        signal,
      }),
    enabled: kind === 'OFFER',
  })
  if (kind === 'NONE' || kind === 'ALL') return null
  if (kind === 'ITEM')
    return (
      <ItemPicker
        id="promotion-item"
        label="Item"
        value={value}
        onChange={onChange}
        placeholder="Choose item"
        disabled={!can(P.ITEM_VIEW)}
      />
    )
  const options =
    kind === 'CATEGORY'
      ? categories.data?.map((entry) => ({ id: entry.id, title: entry.name }))
      : kind === 'BRAND'
        ? brands.data?.map((entry) => ({ id: entry.id, title: entry.name }))
        : offers.data?.data.map((entry) => ({ id: entry.id, title: entry.title }))
  return (
    <div className="space-y-2">
      {kind === 'OFFER' && (
        <div>
          <Label htmlFor="promotion-offer-search">Search offers</Label>
          <Input
            id="promotion-offer-search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            className="mt-1"
            maxLength={120}
          />
        </div>
      )}
      <Label htmlFor="promotion-target">{humanise(kind)} target</Label>
      <select
        id="promotion-target"
        className="min-h-11 w-full rounded-md border bg-background px-3 text-sm"
        value={value || ''}
        onChange={(event) => onChange(event.target.value || null)}
      >
        <option value="">Choose target</option>
        {value && !options?.some((option) => option.id === value) && (
          <option value={value}>Current target</option>
        )}
        {options?.map((option) => (
          <option key={option.id} value={option.id}>
            {option.title}
          </option>
        ))}
      </select>
    </div>
  )
}

function PromotionForm({
  kind,
  row,
  onClose,
}: {
  kind: Kind
  row?: Promotion
  onClose: () => void
}) {
  const { can } = useAuth()
  const client = useQueryClient()
  const existingBanner = row && 'image_document_id' in row ? row : undefined
  const existingOffer = row && 'scope_kind' in row ? row : undefined
  const [targetKind, setTargetKind] = useState(
    existingBanner?.link_kind || existingOffer?.scope_kind || (kind === 'banners' ? 'NONE' : 'ALL'),
  )
  const [targetId, setTargetId] = useState<string | null>(
    existingBanner?.link_id ||
      existingOffer?.item_id ||
      existingOffer?.category_id ||
      existingOffer?.brand_id ||
      null,
  )
  const [customerId, setCustomerId] = useState<string | null>(existingOffer?.customer_id || null)
  const [imageId, setImageId] = useState(existingBanner?.image_document_id || '')
  const [imageFile, setImageFile] = useState<File | null>(null)
  const [formError, setFormError] = useState<string>()
  const action = useMutation({
    mutationFn: async (fields: FormData) => {
      let uploadedId = imageId
      if (kind === 'banners') {
        const file = fields.get('image')
        if (file instanceof File && file.size) {
          if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type))
            throw new Error('Choose a JPEG, PNG or WebP image.')
          const upload = new FormData()
          upload.set('file', file)
          upload.set('category', 'ATTACHMENT')
          // A banner does not exist yet. Its service authorizes the unbound
          // uploaded image by tenant before assigning the document reference.
          const document = await api.post<SchemaOf<'DocumentRead'>>('/documents', upload)
          uploadedId = document.id
          setImageId(uploadedId)
        }
      }
      const payload =
        kind === 'banners'
          ? bannerPayload(fields, uploadedId, targetId)
          : offerPayload(fields, targetId, customerId)
      if (row) {
        const { code: _code, ...editable } = payload
        // Lifecycle changes have their own reasoned action, never a form default.
        const update = { ...editable } as Record<string, unknown>
        delete update.status
        delete update.is_active
        return api.patch(`/promotions/${kind}/${row.id}`, update)
      }
      return api.post(`/promotions/${kind}`, payload)
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['promotions'] })
      void client.invalidateQueries({ queryKey: queryKeys.dealerPortal.all })
      toast.success(row ? success.updated('Promotion') : success.created('Promotion'))
      onClose()
    },
    onError: (error) => setFormError(fieldErrorSummary(error) || describeError(error)),
  })
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !action.isPending) onClose()
      }}
    >
      <DialogContent size="lg">
        <form
          onSubmit={(event) => {
            event.preventDefault()
            setFormError(undefined)
            const fields = new FormData(event.currentTarget)
            if (imageFile) fields.set('image', imageFile)
            try {
              if (kind === 'offers') offerPayload(fields, targetId, customerId)
              else
                bannerPayload(
                  fields,
                  imageId ||
                    (fields.get('image') instanceof File && (fields.get('image') as File).size
                      ? 'pending-upload'
                      : ''),
                  targetId,
                )
              action.mutate(fields)
            } catch (error) {
              setFormError((error as Error).message)
            }
          }}
        >
          <DialogHeader>
            <DialogTitle>
              {row ? 'Edit' : 'New'} {kind === 'banners' ? 'banner' : 'offer'}
            </DialogTitle>
            <DialogDescription>
              Save the content, then activate it with a reason when it is ready to publish.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="promotion-code">Code</Label>
                <Input
                  id="promotion-code"
                  name="code"
                  defaultValue={row?.code || ''}
                  readOnly={Boolean(row)}
                  required
                  maxLength={32}
                  className="mt-1"
                />
              </div>
              <div>
                <Label htmlFor="promotion-title">Title</Label>
                <Input
                  id="promotion-title"
                  name="title"
                  defaultValue={row?.title || ''}
                  required
                  maxLength={160}
                  className="mt-1"
                />
              </div>
            </div>
            {kind === 'banners' ? (
              <>
                <div>
                  <Label htmlFor="promotion-subtitle">Subtitle</Label>
                  <Textarea
                    id="promotion-subtitle"
                    name="subtitle"
                    defaultValue={existingBanner?.subtitle || ''}
                    maxLength={2000}
                    className="mt-1"
                  />
                </div>
                <div>
                  <Label htmlFor="promotion-image">Banner image</Label>
                  <Input
                    id="promotion-image"
                    name="image"
                    type="file"
                    accept="image/jpeg,image/png,image/webp"
                    onChange={(event) => setImageFile(event.target.files?.[0] ?? null)}
                    disabled={!can(P.DOCUMENT_UPLOAD)}
                    className="mt-1"
                  />
                  <p className="mt-1 text-xs text-muted-foreground">
                    {imageId
                      ? 'The current image will be kept unless you upload a replacement.'
                      : 'Choose a JPEG, PNG or WebP image.'}
                  </p>
                  {!can(P.DOCUMENT_UPLOAD) && (
                    <p className="mt-1 text-sm text-muted-foreground">
                      File upload access is required to add a banner image.
                    </p>
                  )}
                </div>
              </>
            ) : (
              <div>
                <Label htmlFor="promotion-description">Description</Label>
                <Textarea
                  id="promotion-description"
                  name="description"
                  defaultValue={existingOffer?.description || ''}
                  maxLength={2000}
                  className="mt-1"
                />
              </div>
            )}
            <div>
              <Label htmlFor="promotion-kind">{kind === 'banners' ? 'Link' : 'Scope'}</Label>
              <select
                id="promotion-kind"
                name={kind === 'banners' ? 'link_kind' : 'scope_kind'}
                value={targetKind}
                onChange={(event) => {
                  setTargetKind(event.target.value as typeof targetKind)
                  setTargetId(null)
                }}
                className="mt-1 min-h-11 w-full rounded-md border bg-background px-3 text-sm"
              >
                {(kind === 'banners'
                  ? ['NONE', 'ITEM', 'CATEGORY', 'BRAND', 'OFFER']
                  : ['ALL', 'ITEM', 'CATEGORY', 'BRAND']
                ).map((value) => (
                  <option key={value} value={value}>
                    {humanise(value)}
                  </option>
                ))}
              </select>
            </div>
            <PromotionTarget kind={targetKind} value={targetId} onChange={setTargetId} />
            {kind === 'offers' && (
              <>
                <CustomerPicker
                  id="promotion-customer"
                  label="Specific customer (optional)"
                  value={customerId}
                  onChange={setCustomerId}
                  disabled={!can(P.CUSTOMER_VIEW)}
                />
                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <Label htmlFor="promotion-minimum">Minimum quantity</Label>
                    <Input
                      id="promotion-minimum"
                      name="min_quantity"
                      inputMode="decimal"
                      defaultValue={existingOffer?.min_quantity || '1'}
                      required
                      className="mt-1"
                    />
                  </div>
                  <div>
                    <Label htmlFor="promotion-discount">Discount %</Label>
                    <Input
                      id="promotion-discount"
                      name="discount_percentage"
                      inputMode="decimal"
                      defaultValue={existingOffer?.discount_percentage || ''}
                      required
                      className="mt-1"
                    />
                  </div>
                </div>
              </>
            )}
            <div>
              <Label htmlFor="promotion-audience">Audience</Label>
              <select
                id="promotion-audience"
                name="audience_price_type"
                defaultValue={row?.audience_price_type || ''}
                className="mt-1 min-h-11 w-full rounded-md border bg-background px-3 text-sm"
              >
                <option value="">All channels</option>
                <option value="SALE">Sale</option>
                <option value="DEALER">Dealer</option>
                <option value="DISTRIBUTOR">Distributor</option>
              </select>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="promotion-from">Valid from</Label>
                <Input
                  id="promotion-from"
                  name="valid_from"
                  type="date"
                  defaultValue={row?.valid_from || ''}
                  className="mt-1"
                />
              </div>
              <div>
                <Label htmlFor="promotion-to">Valid to</Label>
                <Input
                  id="promotion-to"
                  name="valid_to"
                  type="date"
                  defaultValue={row?.valid_to || ''}
                  className="mt-1"
                />
              </div>
            </div>
            <div>
              <Label htmlFor="promotion-sort">Sort order</Label>
              <Input
                id="promotion-sort"
                name="sort_order"
                type="number"
                min={0}
                max={100000}
                step={1}
                defaultValue={row?.sort_order || 0}
                className="mt-1"
              />
            </div>
            <FieldError message={formError} />
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={action.isPending}>
              Cancel
            </Button>
            <Button type="submit" loading={action.isPending}>
              Save {kind === 'banners' ? 'banner' : 'offer'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export default function PromotionsPage() {
  const { can } = useAuth()
  const client = useQueryClient()
  const [params, setParams] = useSearchParams()
  const kind: Kind = params.get('tab') === 'offers' ? 'offers' : 'banners'
  const [editing, setEditing] = useState<Promotion | 'new'>()
  const [pending, setPending] = useState<{ row: Promotion; action: Lifecycle }>()
  const [reason, setReason] = useState('')
  const [reasonError, setReasonError] = useState<string>()
  const list = useListQuery<Promotion>({
    path: `/promotions/${kind}`,
    queryKey: (filters) => ['promotions', kind, filters],
    enabled: can(P.PROMOTION_VIEW),
  })
  const lifecycle = useMutation({
    mutationFn: () =>
      api.post(`/promotions/${kind}/${pending?.row.id}/${pending?.action}`, {
        reason: reason.trim(),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['promotions'] })
      void client.invalidateQueries({ queryKey: queryKeys.dealerPortal.all })
      toast.success(success.updated('Promotion'))
      setPending(undefined)
      setReason('')
    },
    onError: (error) => setReasonError(fieldErrorSummary(error) || describeError(error)),
  })
  const columns = useMemo<ColumnDef<Promotion, unknown>[]>(
    () => [
      ...(kind === 'banners'
        ? [
            {
              id: 'image',
              header: 'Image',
              cell: ({ row }: { row: { original: Promotion } }) =>
                'image_document_id' in row.original ? (
                  <div className="w-24">
                    <DealerImage
                      path={
                        can(P.DOCUMENT_VIEW)
                          ? `/documents/${row.original.image_document_id}/download`
                          : null
                      }
                      label={row.original.title}
                    />
                  </div>
                ) : null,
            },
          ]
        : []),
      { accessorKey: 'code', header: 'Code', enableSorting: false },
      { accessorKey: 'title', header: 'Title', enableSorting: false },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: ({ row }) => <StatusBadge status={row.original.status} />,
        enableSorting: false,
      },
      {
        id: 'validity',
        header: 'Validity',
        cell: ({ row }) =>
          `${formatDate(row.original.valid_from)} – ${formatDate(row.original.valid_to)}`,
      },
      ...(kind === 'offers'
        ? [
            {
              id: 'discount',
              header: 'Discount',
              cell: ({ row }: { row: { original: Promotion } }) =>
                'discount_percentage' in row.original
                  ? `${formatQuantity(row.original.discount_percentage)}%`
                  : '—',
            },
          ]
        : []),
      ...(kind === 'offers'
        ? [
            {
              id: 'preview',
              header: 'Preview',
              cell: ({ row }: { row: { original: Promotion } }) => (
                <OfferPreview id={row.original.id} />
              ),
            },
          ]
        : []),
      {
        id: 'actions',
        header: 'Actions',
        cell: ({ row }) =>
          can(P.PROMOTION_MANAGE) && row.original.status !== 'ARCHIVED' ? (
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" size="sm" onClick={() => setEditing(row.original)}>
                Edit
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setReasonError(undefined)
                  setPending({
                    row: row.original,
                    action: row.original.status === 'ACTIVE' ? 'deactivate' : 'activate',
                  })
                }}
              >
                {row.original.status === 'ACTIVE' ? 'Deactivate' : 'Activate'}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setReasonError(undefined)
                  setPending({ row: row.original, action: 'retire' })
                }}
              >
                Retire
              </Button>
            </div>
          ) : null,
      },
    ],
    [can, kind],
  )
  return (
    <>
      <PageHeader
        title="Promotions"
        description="Manage banners and channel offers shown in the dealer catalog."
        actions={
          can(P.PROMOTION_MANAGE) && (
            <Button onClick={() => setEditing('new')}>
              New {kind === 'banners' ? 'banner' : 'offer'}
            </Button>
          )
        }
      />
      <Tabs
        value={kind}
        onValueChange={(tab) => {
          setEditing(undefined)
          setParams({ tab })
        }}
      >
        <TabsList aria-label="Promotions" className="mb-4">
          {(['banners', 'offers'] as const).map((tab) => (
            <TabsTrigger key={tab} value={tab}>
              {humanise(tab)}
            </TabsTrigger>
          ))}
        </TabsList>
        {(['banners', 'offers'] as const).map((tab) => (
          <TabsContent key={tab} value={tab}>
            <DataTable
              columns={columns}
              data={list.data}
              pagination={list.pagination}
              isLoading={list.isLoading}
              error={list.error}
              onPageChange={list.setPage}
              onLimitChange={list.setLimit}
              onSearchChange={list.setSearch}
              searchValue={list.search}
              onRefresh={() => void list.refetch()}
              emptyTitle={`No ${kind}`}
            />
          </TabsContent>
        ))}
      </Tabs>
      {editing && (
        <PromotionForm
          key={`${kind}-${editing === 'new' ? 'new' : editing.id}`}
          kind={kind}
          row={editing === 'new' ? undefined : editing}
          onClose={() => setEditing(undefined)}
        />
      )}
      <ConfirmDialog
        open={Boolean(pending)}
        onOpenChange={(open) => {
          if (!open) {
            setPending(undefined)
            setReason('')
          }
        }}
        title={`${humanise(pending?.action || 'update')} promotion?`}
        description={pending?.row.title}
        confirmLabel={humanise(pending?.action || 'update')}
        destructive={pending?.action === 'retire'}
        loading={lifecycle.isPending}
        onConfirm={() => {
          if (reason.trim().length < REASON_MIN_LENGTH)
            setReasonError(`Enter at least ${REASON_MIN_LENGTH} characters.`)
          else lifecycle.mutate()
        }}
      >
        <div className="space-y-2">
          <Label htmlFor="promotion-reason">Reason</Label>
          <Textarea
            id="promotion-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            maxLength={REASON_MAX_LENGTH}
          />
          <FieldError message={reasonError} />
        </div>
      </ConfirmDialog>
    </>
  )
}

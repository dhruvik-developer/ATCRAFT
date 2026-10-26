import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { EmptyState, FieldError, PanelLoader } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle, Badge, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { DescriptionPicker } from '@/features/product-descriptions/description-picker'
import {
  invalidateProductDescriptions,
  specificationDisplay,
  specificationDraft,
  specificationReplacement,
  type ItemSpecifications,
  type SpecificationAttribute,
  type SpecificationValueInput,
} from '@/features/product-descriptions/specification-model'
import { SpecificationValueInput as ValueInput } from '@/features/product-descriptions/specification-value-input'
import { ApiError, api } from '@/lib/api-client'
import { fieldErrorSummary } from '@/lib/form-errors'
import { describeError, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

function attributesFrom(read: ItemSpecifications) {
  const attributes = new Map((read.template ?? []).map((row) => [row.attribute.id, row.attribute]))
  for (const value of read.values ?? []) attributes.set(value.attribute.id, value.attribute)
  return [...attributes.values()]
}
function draftsFrom(read: ItemSpecifications) {
  return Object.fromEntries(
    (read.values ?? []).map((value) => [
      value.specification_attribute_id,
      specificationDraft(value),
    ]),
  )
}

function SpecificationEditor({
  initial,
  canEdit,
}: {
  initial: ItemSpecifications
  canEdit: boolean
}) {
  const client = useQueryClient()
  const [attributes, setAttributes] = useState(() => attributesFrom(initial))
  const [drafts, setDrafts] = useState<Record<string, string>>(() => draftsFrom(initial))
  const [errors, setErrors] = useState<Record<string, string>>({})
  const mutation = useMutation({
    mutationFn: (values: SpecificationValueInput[]) =>
      api.put<ItemSpecifications>(`/products/items/${initial.item_id}/specifications`, { values }),
    onSuccess: (saved) => {
      setAttributes(attributesFrom(saved))
      setDrafts(draftsFrom(saved))
      setErrors({})
      client.setQueryData(queryKeys.items.specifications(initial.item_id), saved)
      invalidateProductDescriptions(client)
      toast.success(success.saved('Item specifications'))
    },
    onError: (error, values) => {
      if (!(error instanceof ApiError)) return
      const fields: Record<string, string> = {}
      for (const [path, messages] of Object.entries(error.fieldErrors)) {
        const match = /^values\.(\d+)(?:\.|$)/.exec(path)
        const value = match ? values[Number(match[1])] : undefined
        if (value) fields[value.specification_attribute_id] = messages.join(' ')
      }
      setErrors(fields)
    },
  })
  const template = initial.template ?? []
  const missing = template.filter(
    (row) => row.is_required && !(drafts[row.attribute.id] ?? '').trim(),
  )
  const requiredIds = new Set(
    template.filter((row) => row.is_required).map((row) => row.attribute.id),
  )
  return (
    <form
      className="space-y-5"
      onSubmit={(event) => {
        event.preventDefault()
        if (!canEdit || mutation.isPending) return
        const replacement = specificationReplacement(attributes, drafts)
        setErrors(replacement.errors)
        if (!Object.keys(replacement.errors).length) mutation.mutate(replacement.values)
      }}
    >
      {missing.length > 0 && (
        <Alert variant="warning">
          <AlertTitle>Required specifications are missing</AlertTitle>
          <AlertDescription>
            {missing.map((row) => row.attribute.name).join(', ')}. This is a completeness warning;
            you can still save the item and its specifications.
          </AlertDescription>
        </Alert>
      )}
      {!attributes.length && (
        <EmptyState
          title="No specifications recorded"
          description="The category has no template. Add technical descriptions from the available specification vocabulary."
        />
      )}
      <div className="grid gap-4 md:grid-cols-2">
        {attributes.map((attribute) => {
          const value = initial.values?.find(
            (row) => row.specification_attribute_id === attribute.id,
          )
          return (
            <div key={attribute.id} className="min-w-0 space-y-2 rounded-lg border p-4">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <Label
                  htmlFor={`item-specification-${attribute.id}`}
                  className="break-words font-medium"
                >
                  {attribute.name}
                  {attribute.unit ? ` (${attribute.unit})` : ''}
                </Label>
                {requiredIds.has(attribute.id) && (
                  <Badge variant="warning">Required by category</Badge>
                )}
              </div>
              {canEdit ? (
                <ValueInput
                  attribute={attribute}
                  id={`item-specification-${attribute.id}`}
                  value={drafts[attribute.id] ?? ''}
                  disabled={mutation.isPending}
                  invalid={Boolean(errors[attribute.id])}
                  onChange={(draft) => {
                    setDrafts((current) => ({ ...current, [attribute.id]: draft }))
                    setErrors((current) => ({ ...current, [attribute.id]: '' }))
                    if (mutation.error) mutation.reset()
                  }}
                />
              ) : (
                <p>{value ? specificationDisplay(value) : 'Not recorded'}</p>
              )}
              <FieldError message={errors[attribute.id]} />
              {canEdit && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={mutation.isPending || !(drafts[attribute.id] ?? '').trim()}
                  onClick={() => setDrafts((current) => ({ ...current, [attribute.id]: '' }))}
                  aria-label={`Clear ${attribute.name}`}
                >
                  Clear value
                </Button>
              )}
            </div>
          )
        })}
      </div>
      {canEdit && (
        <DescriptionPicker<SpecificationAttribute>
          label="Specification"
          path={`/products/items/${initial.item_id}/specification-attributes`}
          queryKey={(params) => queryKeys.items.specificationOptions(initial.item_id, params)}
          excludedIds={attributes.map((attribute) => attribute.id)}
          disabled={mutation.isPending}
          onChoose={(attribute) => setAttributes((current) => [...current, attribute])}
        />
      )}
      <FieldError message={errors._root} />
      {mutation.error && (
        <FieldError message={fieldErrorSummary(mutation.error) || describeError(mutation.error)} />
      )}
      {canEdit && (
        <div className="flex flex-wrap gap-2">
          <Button type="submit" loading={mutation.isPending}>
            Save specifications
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={mutation.isPending}
            onClick={() => {
              setAttributes(attributesFrom(initial))
              setDrafts(draftsFrom(initial))
              setErrors({})
              mutation.reset()
            }}
          >
            Reset changes
          </Button>
        </div>
      )}
    </form>
  )
}

export function ItemSpecificationsPanel({ itemId }: { itemId: string }) {
  const { can } = useAuth()
  const result = useQuery({
    queryKey: queryKeys.items.specifications(itemId),
    queryFn: ({ signal }) =>
      api.get<ItemSpecifications>(`/products/items/${itemId}/specifications`, { signal }),
  })
  if (result.isLoading) return <PanelLoader />
  if (result.error) return <FieldError message={describeError(result.error)} />
  if (!result.data) return null
  return (
    <section className="space-y-4">
      <p className="text-sm text-muted-foreground">
        Technical descriptions inherited from the item's category and its ancestors, with any
        additional recorded specifications. These values do not change inventory or planning.
      </p>
      <SpecificationEditor key={itemId} initial={result.data} canEdit={can(P.ITEM_UPDATE)} />
    </section>
  )
}

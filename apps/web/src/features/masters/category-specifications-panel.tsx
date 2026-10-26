import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { EmptyState, FieldError, PanelLoader } from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Checkbox, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { DescriptionPicker } from '@/features/product-descriptions/description-picker'
import {
  invalidateProductDescriptions,
  type SpecificationAttribute,
} from '@/features/product-descriptions/specification-model'
import { api } from '@/lib/api-client'
import { PRODUCT_DESCRIPTION_POSITION_MAX, SPECIFICATION_VALUE_MAX } from '@/lib/constants'
import { fieldErrorSummary } from '@/lib/form-errors'
import { describeError, success } from '@/lib/messages'
import { P } from '@/lib/permissions'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

type Category = SchemaOf<'SpecificationCategoryOption'>
type Templates = SchemaOf<'CategorySpecificationsRead'>
type Draft = { attribute: SpecificationAttribute; required: boolean; position: string }

function templateDrafts(read: Templates): Draft[] {
  return read.specifications.map((row) => ({
    attribute: row.attribute,
    required: row.is_required,
    position: String(row.sort_order),
  }))
}

function TemplateEditor({ initial, canManage }: { initial: Templates; canManage: boolean }) {
  const client = useQueryClient()
  const [rows, setRows] = useState(() => templateDrafts(initial))
  const [error, setError] = useState<string>()
  const mutation = useMutation({
    mutationFn: (body: SchemaOf<'CategorySpecificationsReplace'>) =>
      api.put<Templates>(`/products/categories/${initial.category_id}/specifications`, body),
    onSuccess: (saved) => {
      setRows(templateDrafts(saved))
      invalidateProductDescriptions(client)
      toast.success(success.saved('Category template'))
    },
  })
  return (
    <form
      className="mt-5 space-y-4"
      onSubmit={(event) => {
        event.preventDefault()
        setError(undefined)
        if (!canManage || mutation.isPending) return
        if (rows.some((row) => !/^\d{1,4}$/.test(row.position.trim()))) {
          setError(`Positions must be whole numbers from 0 to ${PRODUCT_DESCRIPTION_POSITION_MAX}.`)
          return
        }
        mutation.mutate({
          specifications: rows.map((row) => ({
            specification_attribute_id: row.attribute.id,
            is_required: row.required,
            sort_order: Number(row.position),
          })),
        })
      }}
    >
      {rows.length === 0 && (
        <EmptyState
          title="No category specifications"
          description="Ancestors may still provide inherited specifications. Add the attributes this category needs."
        />
      )}
      <div className="space-y-3">
        {rows.map((row, index) => (
          <div
            key={row.attribute.id}
            className="flex min-w-0 flex-wrap items-center gap-3 rounded-lg border p-3"
          >
            <div className="min-w-0 grow basis-40">
              <p className="break-words font-medium">{row.attribute.name}</p>
              <p className="text-xs text-muted-foreground">
                {row.attribute.code} · {row.attribute.value_type.toLowerCase()}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Checkbox
                id={`required-${row.attribute.id}`}
                checked={row.required}
                disabled={!canManage || mutation.isPending}
                onCheckedChange={(checked) =>
                  setRows((current) =>
                    current.map((value, position) =>
                      position === index ? { ...value, required: checked === true } : value,
                    ),
                  )
                }
              />
              <Label htmlFor={`required-${row.attribute.id}`}>Required warning</Label>
            </div>
            <div className="w-24">
              <Label htmlFor={`position-${row.attribute.id}`}>Position</Label>
              <Input
                id={`position-${row.attribute.id}`}
                inputMode="numeric"
                value={row.position}
                disabled={!canManage || mutation.isPending}
                onChange={(event) =>
                  setRows((current) =>
                    current.map((value, position) =>
                      position === index ? { ...value, position: event.target.value } : value,
                    ),
                  )
                }
              />
            </div>
            {canManage && (
              <Button
                type="button"
                variant="outline"
                disabled={mutation.isPending}
                onClick={() =>
                  setRows((current) =>
                    current.filter((value) => value.attribute.id !== row.attribute.id),
                  )
                }
                aria-label={`Remove ${row.attribute.name}`}
              >
                Remove
              </Button>
            )}
          </div>
        ))}
      </div>
      {canManage && (
        <DescriptionPicker<SpecificationAttribute>
          label="Specification"
          path="/products/specification-attributes"
          queryKey={queryKeys.specifications.attributes}
          params={{ is_active: true }}
          excludedIds={rows.map((row) => row.attribute.id)}
          disabled={mutation.isPending || rows.length >= SPECIFICATION_VALUE_MAX}
          onChoose={(attribute) =>
            setRows((current) => [
              ...current,
              { attribute, required: false, position: String(current.length) },
            ])
          }
        />
      )}
      <FieldError
        message={
          error ||
          (mutation.error
            ? fieldErrorSummary(mutation.error) || describeError(mutation.error)
            : undefined)
        }
      />
      {canManage && (
        <Button type="submit" loading={mutation.isPending}>
          Save category template
        </Button>
      )}
    </form>
  )
}

export function CategorySpecificationsPanel() {
  const { can } = useAuth()
  const [category, setCategory] = useState<Category | null>(null)
  const template = useQuery({
    queryKey: queryKeys.specifications.templates(category?.id ?? ''),
    queryFn: ({ signal }) =>
      api.get<Templates>(`/products/categories/${category?.id}/specifications`, { signal }),
    enabled: Boolean(category),
  })
  return (
    <section className="space-y-4">
      <p className="text-sm text-muted-foreground">
        Select a category and maintain its own template. Items also inherit every ancestor template;
        an inherited required flag remains required. Missing values produce a warning and do not
        prevent saving.
      </p>
      <DescriptionPicker<Category>
        label="Category"
        path="/products/specification-categories"
        queryKey={queryKeys.specifications.categories}
        onChoose={setCategory}
      />
      {category && (
        <h2 className="text-lg font-semibold">
          {category.code} - {category.name}
        </h2>
      )}
      {template.isLoading && <PanelLoader />}
      {template.error && <FieldError message={describeError(template.error)} />}
      {template.data && (
        <TemplateEditor
          key={template.data.category_id}
          initial={template.data}
          canManage={can(P.ITEM_ATTRIBUTE_MANAGE)}
        />
      )}
    </section>
  )
}

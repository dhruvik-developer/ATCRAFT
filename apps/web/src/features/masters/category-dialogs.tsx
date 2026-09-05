/**
 * Create, edit and retire an item category.
 *
 * Categories are a tree held as a materialised path, and everything awkward
 * about these two dialogs follows from that:
 *
 * - **The parent is always explicit.** A new child is created with its parent
 *   named in the form, never inferred from whatever the grid happens to be
 *   filtered to, because the grid's filter and the row the user right-clicked
 *   are not the same thing.
 * - **Moving a node moves its subtree.** `PATCH` rewrites every descendant path
 *   in one statement, so the dialog says so before the user presses save.
 * - **`parent_id: null` does not clear the parent.** `update_category` skips
 *   null fields, so promoting a node to the top level goes through the
 *   contract's dedicated `reparent_to_root` flag instead.
 *
 * Both writes are gated on `category.manage`; the caller renders neither dialog
 * without it, and the API enforces it regardless (master rules 38).
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { z } from 'zod'

import { ConfirmDialog, FieldError } from '@/components/shared'
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
  Checkbox,
  Input,
  Label,
  Textarea,
} from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT, SELECT_NONE } from '@/lib/constants'
import { applyFieldErrors } from '@/lib/form-errors'
import { success } from '@/lib/messages'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import {
  eligibleParents,
  mastersKeys,
  useCategoryReference,
  type CategoryRow,
} from './masters-model'

const schema = z.object({
  code: z.string().trim().min(1, 'Code is required').max(32, 'Code is at most 32 characters'),
  name: z.string().trim().min(1, 'Name is required').max(255, 'Name is at most 255 characters'),
  description: z.string(),
  parent_id: z.string(),
  is_active: z.boolean(),
})

type CategoryFormValues = z.infer<typeof schema>

function emptyValues(parentId: string | null): CategoryFormValues {
  return {
    code: '',
    name: '',
    description: '',
    parent_id: parentId ?? SELECT_NONE,
    is_active: true,
  }
}

function valuesFrom(category: CategoryRow): CategoryFormValues {
  return {
    code: category.code,
    name: category.name,
    description: category.description ?? '',
    parent_id: category.parent_id ?? SELECT_NONE,
    is_active: category.is_active,
  }
}

export function CategoryFormDialog({
  open,
  onOpenChange,
  category,
  defaultParentId,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present for an edit, absent for a create. */
  category?: CategoryRow | null
  /** Pre-selected parent when the create was raised from a row's "Add child". */
  defaultParentId?: string | null
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(category)

  // Loaded only while the dialog is open: a parent list nobody is looking at is
  // a request nobody asked for.
  const reference = useCategoryReference(open)
  const parents = eligibleParents(reference.data?.rows ?? [], category ?? null)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    watch,
    formState: { errors },
  } = useForm<CategoryFormValues>({
    resolver: zodResolver(schema),
    defaultValues: emptyValues(defaultParentId ?? null),
  })

  // Seeded once per opening and keyed on the row's id rather than its identity:
  // a background refetch of the list would otherwise resolve mid-edit and wipe
  // what the user is typing.
  const editingId = category?.id ?? null
  useEffect(() => {
    if (open) reset(category ? valuesFrom(category) : emptyValues(defaultParentId ?? null))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, defaultParentId, reset])

  const mutation = useMutation({
    mutationFn: (values: CategoryFormValues) => {
      const parentId = values.parent_id === SELECT_NONE ? null : values.parent_id

      if (category) {
        return api.patch<CategoryRow>(`/products/categories/${category.id}`, {
          name: values.name.trim(),
          // An empty string, never null: the PATCH drops nulls, so `""` is the
          // only way to clear a description that is no longer true.
          description: values.description.trim(),
          is_active: values.is_active,
          // Promotion to the top level is a flag of its own, for the same
          // reason - a null `parent_id` would simply be ignored.
          ...(parentId === null ? { reparent_to_root: true } : { parent_id: parentId }),
        })
      }

      return api.post<CategoryRow>('/products/categories', {
        code: values.code.trim(),
        name: values.name.trim(),
        description: values.description.trim() === '' ? null : values.description.trim(),
        parent_id: parentId,
        is_active: values.is_active,
      })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: mastersKeys.all })
      // The item form and every other category picker read the shared branch,
      // so a category added here must not stay invisible to them for the half
      // hour that reference data is allowed to be stale.
      void queryClient.invalidateQueries({ queryKey: queryKeys.categories.all })
      toast.success(
        isEdit ? success.updated('Category') : success.created('Category'),
        `${saved.code} - ${saved.name}`,
      )
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  const selectedParent = watch('parent_id')
  const parentChanged = isEdit && selectedParent !== (category?.parent_id ?? SELECT_NONE)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit ${category?.code}` : 'New category'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'The code cannot be changed - historical reports group on it.'
              : 'Categories nest. Choose the parent this one sits under, or leave it at the top level.'}
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="category-code" required>
                  Code
                </Label>
                <Input
                  id="category-code"
                  {...register('code')}
                  disabled={isEdit}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.code)}
                  className="mt-1.5"
                />
                <FieldError message={errors.code?.message} />
              </div>

              <div>
                <Label htmlFor="category-name" required>
                  Name
                </Label>
                <Input
                  id="category-name"
                  {...register('name')}
                  autoComplete="off"
                  aria-invalid={Boolean(errors.name)}
                  className="mt-1.5"
                />
                <FieldError message={errors.name?.message} />
              </div>
            </div>

            <div>
              <Label htmlFor="category-parent">Parent category</Label>
              <Controller
                control={control}
                name="parent_id"
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id="category-parent" className="mt-1.5">
                      <SelectValue placeholder="Top level" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={SELECT_NONE}>Top level (no parent)</SelectItem>
                      {parents.map((row) => (
                        <SelectItem key={row.id} value={row.id}>
                          {/* Indented by depth: the list arrives in path order,
                              so the indentation is the tree. */}
                          <span style={{ paddingLeft: `${row.depth * 0.75}rem` }}>
                            {row.code} - {row.name}
                            {!row.is_active && ' (inactive)'}
                          </span>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
              <FieldError message={errors.parent_id?.message} />
              {isEdit && (
                <p className="mt-1 text-xs text-muted-foreground">
                  A category cannot be moved beneath one of its own descendants, so its own subtree
                  is not offered here.
                </p>
              )}
              {reference.data?.truncated && (
                <p className="mt-1 text-xs text-warning">
                  Only the first {LOOKUP_LIST_LIMIT} categories are listed. Narrow the tree before
                  re-parenting a branch that is not shown.
                </p>
              )}
            </div>

            <div>
              <Label htmlFor="category-description">Description</Label>
              <Textarea
                id="category-description"
                {...register('description')}
                rows={3}
                className="mt-1.5"
              />
              <FieldError message={errors.description?.message} />
            </div>

            <div className="flex items-start gap-2.5">
              <Controller
                control={control}
                name="is_active"
                render={({ field }) => (
                  <Checkbox
                    id="category-active"
                    checked={field.value}
                    onCheckedChange={(checked) => field.onChange(checked === true)}
                  />
                )}
              />
              <div>
                <Label htmlFor="category-active" className="cursor-pointer font-normal">
                  Available on new items
                </Label>
                <p className="text-xs text-muted-foreground">
                  Turning this off hides the category from the item form. Items already classified
                  under it keep their classification.
                </p>
              </div>
            </div>

            {parentChanged && (
              <Alert variant="warning">
                <AlertDescription>
                  Moving this category moves everything beneath it. Every descendant is re-levelled
                  in the same transaction, and the move is refused if it would breach the nesting
                  limit.
                </AlertDescription>
              </Alert>
            )}

            <FieldError message={errors.root?.message} />
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={mutation.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={mutation.isPending}>
              {isEdit ? 'Save changes' : 'Create category'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/**
 * Retire a category.
 *
 * The API refuses this while the node still has sub-categories or classified
 * items - reassigning them silently would change how every historical report
 * groups them - so the dialog states both conditions rather than letting the
 * user discover them one rejection at a time.
 */
export function CategoryRetireDialog({
  category,
  onOpenChange,
}: {
  category: CategoryRow | null
  onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: (target: CategoryRow) => api.delete(`/products/categories/${target.id}`),
    onSuccess: (_result, target) => {
      void queryClient.invalidateQueries({ queryKey: mastersKeys.all })
      void queryClient.invalidateQueries({ queryKey: queryKeys.categories.all })
      toast.success(success.retired('Category'), `${target.code} - ${target.name}`)
      onOpenChange(false)
    },
  })

  return (
    <ConfirmDialog
      open={Boolean(category)}
      onOpenChange={onOpenChange}
      title="Retire this category?"
      description={
        category
          ? `${category.code} - ${category.name} disappears from the tree and from every picker. It is refused while the category still has sub-categories or items assigned to it.`
          : undefined
      }
      confirmLabel="Retire category"
      destructive
      loading={mutation.isPending}
      onConfirm={() => category && mutation.mutate(category)}
    />
  )
}

/**
 * Create and edit a role.
 *
 * Two fields are missing on purpose, because the API refuses them rather than
 * ignoring them:
 *
 * * `code` cannot be changed - it appears in seed data, in the principal's role
 *   list and in historical audit rows, so renaming it would make that history
 *   ambiguous.
 * * `is_vendor_role` cannot be flipped - one statement would either strip the
 *   portal boundary from every holder or make every holder illegal.
 *
 * The permission set is edited in the matrix, not here: it is a few hundred
 * checkboxes and belongs on a screen of its own.
 */

import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { Controller, useForm } from 'react-hook-form'
import { z } from 'zod'

import { FieldError } from '@/components/shared'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/overlays'
import { Checkbox, Input, Label, Textarea } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { applyFieldErrors } from '@/lib/form-errors'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import type { RoleDetail, RoleRow } from './admin-types'

const ROLE_CODE_PATTERN = /^[A-Za-z][A-Za-z0-9_ -]*$/

const roleSchema = z.object({
  code: z
    .string()
    .trim()
    .min(2, 'Code is at least 2 characters')
    .max(48, 'At most 48 characters')
    .regex(
      ROLE_CODE_PATTERN,
      'Start with a letter; letters, digits, spaces, dashes and underscores only',
    ),
  name: z.string().trim().min(1, 'Name is required').max(255, 'At most 255 characters'),
  description: z.string().trim().max(2000, 'At most 2000 characters'),
  is_vendor_role: z.boolean(),
  is_active: z.boolean(),
})

type RoleFormValues = z.infer<typeof roleSchema>

function emptyRole(): RoleFormValues {
  return { code: '', name: '', description: '', is_vendor_role: false, is_active: true }
}

function roleValues(role: RoleRow): RoleFormValues {
  return {
    code: role.code,
    name: role.name,
    description: role.description ?? '',
    is_vendor_role: role.is_vendor_role,
    is_active: role.is_active,
  }
}

export function RoleFormDialog({
  open,
  onOpenChange,
  role,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Present for an edit, absent for a create. */
  role?: RoleRow | null
}) {
  const queryClient = useQueryClient()
  const isEdit = Boolean(role)

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<RoleFormValues>({
    resolver: zodResolver(roleSchema),
    defaultValues: emptyRole(),
  })

  const editingId = role?.id ?? null
  useEffect(() => {
    if (open) reset(role ? roleValues(role) : emptyRole())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingId, reset])

  const mutation = useMutation({
    mutationFn: (values: RoleFormValues) => {
      const description = values.description.trim() || null
      return role
        ? api.patch<RoleDetail>(`/roles/${role.id}`, {
            name: values.name.trim(),
            description,
            is_active: values.is_active,
          })
        : api.post<RoleDetail>('/roles', {
            code: values.code.trim(),
            name: values.name.trim(),
            description,
            is_vendor_role: values.is_vendor_role,
            is_active: values.is_active,
            // A new role starts with nothing; the matrix is where authority is
            // handed out, one considered checkbox at a time.
            permissions: [],
          })
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.users.all })
      toast.success(isEdit ? 'Role updated' : 'Role created', `${saved.code} - ${saved.name}`)
      onOpenChange(false)
    },
    onError: (error) => applyFieldErrors(error, setError),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit ${role?.code}` : 'New role'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'The code and the vendor flag are fixed once a role exists.'
              : 'The role is created empty. Grant it permissions on the Permissions tab.'}
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
          className="flex min-h-0 flex-col gap-4"
          noValidate
        >
          <DialogBody className="space-y-4">
            <div>
              <Label htmlFor="role-code" required>
                Code
              </Label>
              <Input
                id="role-code"
                {...register('code')}
                disabled={isEdit}
                autoComplete="off"
                aria-invalid={Boolean(errors.code)}
                className="mt-1.5 font-mono"
              />
              <FieldError message={errors.code?.message} />
              <p className="mt-1 text-xs text-muted-foreground">
                Normalised to capitals and underscores by the server, for example PURCHASE_MANAGER.
              </p>
            </div>

            <div>
              <Label htmlFor="role-name" required>
                Name
              </Label>
              <Input
                id="role-name"
                {...register('name')}
                autoComplete="off"
                aria-invalid={Boolean(errors.name)}
                className="mt-1.5"
              />
              <FieldError message={errors.name?.message} />
            </div>

            <div>
              <Label htmlFor="role-description">Description</Label>
              <Textarea id="role-description" {...register('description')} className="mt-1.5" />
              <FieldError message={errors.description?.message} />
            </div>

            {!isEdit && (
              <label className="flex items-start gap-2.5 text-sm">
                <Controller
                  control={control}
                  name="is_vendor_role"
                  render={({ field }) => (
                    <Checkbox checked={field.value} onCheckedChange={field.onChange} />
                  )}
                />
                <span>
                  Vendor portal role
                  <span className="block text-xs text-muted-foreground">
                    Assignable only to accounts bound to a vendor. This cannot be changed later.
                  </span>
                </span>
              </label>
            )}

            <label className="flex items-start gap-2.5 text-sm">
              <Controller
                control={control}
                name="is_active"
                render={({ field }) => (
                  <Checkbox checked={field.value} onCheckedChange={field.onChange} />
                )}
              />
              <span>
                Available for assignment
                <span className="block text-xs text-muted-foreground">
                  An inactive role grants nothing, even to accounts that still hold it.
                </span>
              </span>
            </label>
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={mutation.isPending}>
              {isEdit ? 'Save changes' : 'Create role'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

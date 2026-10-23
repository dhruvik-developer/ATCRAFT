/**
 * The small components every tab on the item-masters screen shares.
 *
 * Moved out of `masters-page.tsx` when the brand and manufacturer tabs arrived
 * as their own lazily loaded modules: a tab that imported these from the page
 * would tie its chunk back to the page's, and a second copy in each tab is how
 * two tabs come to filter the same URL keys two different ways. The URL-writing
 * hook beside them lives in `use-filter-writer.ts`.
 */

import { useEffect, useState, type ReactNode } from 'react'

import { ConfirmDialog, FieldError } from '@/components/shared'
import {
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
  Input,
  Label,
} from '@/components/ui/primitives'
import { REASON_MAX_LENGTH, REASON_MIN_LENGTH, SELECT_ALL } from '@/lib/constants'
import { describeError, messages } from '@/lib/messages'

/** Active / inactive, the same three-way control on most of the tabs. */
export function StatusFilter({
  value,
  onChange,
  label,
}: {
  value: string
  onChange: (next: string | undefined) => void
  label: string
}) {
  return (
    <Select
      value={value || SELECT_ALL}
      onValueChange={(next) => onChange(next === SELECT_ALL ? undefined : next)}
    >
      <SelectTrigger className="w-full sm:w-[150px]" aria-label={label}>
        <SelectValue placeholder="Any status" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={SELECT_ALL}>Any status</SelectItem>
        <SelectItem value="active">Active</SelectItem>
        <SelectItem value="inactive">Inactive</SelectItem>
      </SelectContent>
    </Select>
  )
}

export function ActiveBadge({ isActive }: { isActive: boolean }) {
  return isActive ? (
    <Badge variant="secondary">Active</Badge>
  ) : (
    <Badge variant="muted">Inactive</Badge>
  )
}

/** The sentence that introduces a tab, above its table. */
export function TabIntro({ children, actions }: { children: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-4 mt-4 flex flex-wrap items-start justify-between gap-3">
      <p className="max-w-3xl text-sm text-muted-foreground">{children}</p>
      {actions && (
        <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">{actions}</div>
      )}
    </div>
  )
}

/**
 * The confirmation for a retirement that must carry a reason.
 *
 * Presentational on purpose: the caller owns the mutation, so the DELETE it
 * sends - and the permission that gates it - stays visible in the feature file
 * that offers it. The API's refusal is shown verbatim: "still carried by live
 * items" names exactly what has to change, and rewording it here would lose
 * that.
 */
export function RetireWithReasonDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  inputId,
  pending,
  error,
  errorHeadline,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: ReactNode
  confirmLabel: string
  inputId: string
  pending: boolean
  /** The failed attempt, if any. Cleared by the caller when the dialog closes. */
  error: Error | null
  errorHeadline: string
  onConfirm: (reason: string) => void
}) {
  const [reason, setReason] = useState('')
  const [touched, setTouched] = useState(false)
  const tooShort = reason.trim().length < REASON_MIN_LENGTH

  // Reset on close rather than on open, so a refused attempt stays readable -
  // and correctable - for as long as the dialog is up.
  useEffect(() => {
    if (!open) {
      setReason('')
      setTouched(false)
    }
  }, [open])

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      description={description}
      confirmLabel={confirmLabel}
      destructive
      loading={pending}
      onConfirm={() => {
        if (tooShort) {
          setTouched(true)
          return
        }
        onConfirm(reason.trim())
      }}
    >
      <div className="space-y-3">
        {error && (
          <Alert variant="destructive">
            <AlertTitle>{errorHeadline}</AlertTitle>
            <AlertDescription>{describeError(error)}</AlertDescription>
          </Alert>
        )}

        <div>
          <Label htmlFor={inputId} required>
            Reason
          </Label>
          <Input
            id={inputId}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            maxLength={REASON_MAX_LENGTH}
            autoComplete="off"
            className="mt-1.5"
            aria-invalid={touched && tooShort}
          />
          {touched && tooShort ? (
            <FieldError message={messages.errors.reasonTooShort} />
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">{messages.prompts.reasonRequired}</p>
          )}
        </div>
      </div>
    </ConfirmDialog>
  )
}

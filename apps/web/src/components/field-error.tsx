/**
 * The one presentation of a field-level error: a small destructive line under
 * the control it is about, announced as it appears.
 *
 * It lives in its own module, and `shared.tsx` re-exports it, so the eagerly
 * loaded sign-in screen can use it without importing `shared.tsx` - which would
 * put the shared chunk, and the Radix overlay families inside it, on the first
 * paint every visitor waits for. Every other screen imports it from
 * `@/components/shared` as before.
 */

import { joinValidationMessages } from '@/lib/messages'
import { cn } from '@/lib/utils'

export function FieldError({
  message,
  id,
  className,
}: {
  message?: string | string[]
  /** For the control's `aria-describedby`, so the error is read as its description. */
  id?: string
  className?: string
}) {
  if (!message) return null
  const text = Array.isArray(message) ? joinValidationMessages(message) : message
  return (
    <p id={id} role="alert" className={cn('mt-1 text-xs text-destructive', className)}>
      {text}
    </p>
  )
}

import { AlertTriangle, CheckCircle2, Info, X, XCircle } from 'lucide-react'

import { dismiss, useToasts, type ToastVariant } from '@/lib/toast'
import { cn } from '@/lib/utils'

const icons: Record<ToastVariant, typeof Info> = {
  default: Info,
  success: CheckCircle2,
  warning: AlertTriangle,
  error: XCircle,
}

const styles: Record<ToastVariant, string> = {
  default: 'border-border bg-popover',
  success: 'border-success/40 bg-popover',
  warning: 'border-warning/40 bg-popover',
  error: 'border-destructive/40 bg-popover',
}

const iconStyles: Record<ToastVariant, string> = {
  default: 'text-muted-foreground',
  success: 'text-success',
  warning: 'text-warning',
  error: 'text-destructive',
}

export function Toaster() {
  const toasts = useToasts()

  return (
    <div
      // aria-live so a screen reader announces a failed save; the region itself
      // must exist in the DOM before the message arrives, hence it always renders.
      role="region"
      aria-live="polite"
      aria-label="Notifications"
      className="pointer-events-none fixed bottom-0 right-0 z-[100] flex w-full max-w-sm flex-col gap-2 p-4"
    >
      {toasts.map((item) => {
        const Icon = icons[item.variant]
        return (
          <div
            key={item.id}
            role={item.variant === 'error' ? 'alert' : 'status'}
            data-toast-variant={item.variant}
            className={cn(
              'pointer-events-auto flex items-start gap-3 rounded-lg border p-3.5 shadow-lg',
              'animate-in slide-in-from-right-4 fade-in-0',
              styles[item.variant],
            )}
          >
            <Icon className={cn('mt-0.5 h-4 w-4 shrink-0', iconStyles[item.variant])} aria-hidden />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium leading-snug">{item.title}</p>
              {item.description && (
                <p className="mt-0.5 break-words text-sm text-muted-foreground">
                  {item.description}
                </p>
              )}
              {item.action && (
                <button
                  type="button"
                  onClick={() => {
                    item.action?.onClick()
                    dismiss(item.id)
                  }}
                  className="mt-2 text-sm font-medium text-primary underline-offset-2 hover:underline"
                >
                  {item.action.label}
                </button>
              )}
            </div>
            <button
              type="button"
              onClick={() => dismiss(item.id)}
              className="shrink-0 rounded-sm p-0.5 opacity-50 transition-opacity hover:opacity-100"
              aria-label="Dismiss"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        )
      })}
    </div>
  )
}

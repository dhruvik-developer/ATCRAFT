import { Check } from 'lucide-react'

import { cn } from '@/lib/utils'

/**
 * Where a document stands in its lifecycle: one pill per stage, the stages it
 * has passed ticked, and the one it is at marked `aria-current="step"` - which
 * is what tells a screen-reader user where the document is; colour alone would
 * not.
 *
 * `current` indexes `stages`. A `stopped` document left the lifecycle - it was
 * rejected or cancelled - so no stage is drawn as reached. A page with more to
 * say about why draws its own notice instead of the stepper: a cancelled
 * production order explains what happened to the material it had issued.
 *
 * Its own module rather than `components/shared.tsx`, which the app shell loads
 * on the first paint: only a document's detail page draws a lifecycle, and every
 * one of those is a lazy route.
 */
export function LifecycleStepper({
  stages,
  current,
  stopped = false,
  label,
}: {
  stages: readonly string[]
  current: number
  stopped?: boolean
  /** Names the list for assistive technology: "Purchase order lifecycle". */
  label: string
}) {
  return (
    <ol className="flex flex-wrap items-center gap-x-1 gap-y-2" aria-label={label}>
      {stages.map((stage, index) => {
        const done = !stopped && current > index
        const active = !stopped && current === index
        return (
          <li key={stage} className="flex items-center gap-1">
            <span
              aria-current={active ? 'step' : undefined}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium',
                done && 'border-success/40 bg-success/10 text-success',
                active && 'border-primary bg-primary/10 text-primary',
                !done && !active && 'border-border text-muted-foreground',
              )}
            >
              {done && <Check className="h-3 w-3" aria-hidden />}
              {stage}
            </span>
            {index < stages.length - 1 && (
              <span className={cn('h-px w-4', done ? 'bg-success/50' : 'bg-border')} aria-hidden />
            )}
          </li>
        )
      })}
    </ol>
  )
}

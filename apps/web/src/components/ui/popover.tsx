import * as PopoverPrimitive from '@radix-ui/react-popover'
import { forwardRef } from 'react'

import { cn } from '@/lib/utils'

// --------------------------------------------------------------- Popover ----

export const Popover = PopoverPrimitive.Root
export const PopoverTrigger = PopoverPrimitive.Trigger

export const PopoverContent = forwardRef<
  React.ElementRef<typeof PopoverPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof PopoverPrimitive.Content>
>(({ className, align = 'center', sideOffset = 4, collisionPadding = 8, ...props }, ref) => (
  <PopoverPrimitive.Portal>
    <PopoverPrimitive.Content
      ref={ref}
      align={align}
      sideOffset={sideOffset}
      collisionPadding={collisionPadding}
      className={cn(
        // Capped at what Radix says is available, so a w-72 (or wider) popover
        // opened near the edge of a 360px phone shrinks instead of being
        // clipped; collisionPadding keeps it off the viewport edge.
        'z-50 w-72 max-w-[var(--radix-popover-content-available-width)] rounded-md border bg-popover p-4 text-popover-foreground shadow-md outline-none',
        'data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0',
        className,
      )}
      {...props}
    />
  </PopoverPrimitive.Portal>
))
PopoverContent.displayName = 'PopoverContent'

import { Star } from 'lucide-react'

import { Badge } from '@/components/ui/primitives'

/**
 * The marker on the one default of its kind: a bill of materials, a routing, a
 * warehouse, an address.
 *
 * One look everywhere. The same fact - this is the item's default bill - read
 * green on the bill, grey on the item's where-used panel and in the brand
 * colour on the warehouse list, so a reader comparing two screens saw three
 * different states. Kept out of `shared.tsx`, which the first paint loads: only
 * the lazy master screens draw it.
 */
export function DefaultBadge() {
  return (
    <Badge variant="success">
      <Star className="h-3 w-3" aria-hidden />
      Default
    </Badge>
  )
}

/**
 * Radix-backed overlay primitives: dialog, select, dropdown menu, tooltip, tabs.
 *
 * Radix is used rather than hand-rolled overlays because focus trapping, escape
 * handling, scroll locking and ARIA wiring are exactly the things that get
 * quietly wrong in a bespoke implementation - and this application is operated
 * from the keyboard all day.
 *
 * This file is a re-export barrel; each family lives in its own module beside
 * it. A module is the unit Rollup can place, so while all six shared one file
 * every screen that wanted a dropdown menu got Select, Dialog, Popover and Tabs
 * with it - and because the app shell imports a dropdown menu, all of it landed
 * in the eager entry chunk that every visitor downloads before anything
 * renders, roughly a fifth of it for code only the lazy feature dialogs use.
 * Split, each primitive is placed with the chunks that actually import it.
 *
 * Import sites are unchanged: `@/components/ui/overlays` still exports
 * everything. Importing the specific module is marginally better still, and is
 * what the eager shell does.
 *
 * Re-exports are spelled out rather than `export *` because the Fast Refresh
 * lint rule cannot see through a star and warns on every one of them.
 */

export {
  Dialog,
  DialogTrigger,
  DialogClose,
  DialogContent,
  DialogHeader,
  DialogFooter,
  DialogTitle,
  DialogDescription,
  DialogBody,
} from './dialog'

export {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuGroup,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuCheckboxItem,
} from './dropdown-menu'

export { Popover, PopoverTrigger, PopoverContent } from './popover'

export {
  Select,
  SelectGroup,
  SelectValue,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectLabel,
  SelectSeparator,
} from './select'

export { Tabs, TabsList, TabsTrigger, TabsContent } from './tabs'

export { TooltipProvider, Tooltip, TooltipTrigger, TooltipContent } from './tooltip'

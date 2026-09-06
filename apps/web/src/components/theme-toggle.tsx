/**
 * Choosing the theme: light, dark, or whatever the operating system says.
 *
 * A three-state segmented control rather than a menu, deliberately. Every state
 * is one click away and visibly which-one-is-current, where a menu costs a click
 * to see the answer; and it introduces no popper, which matters here because
 * this control sits in the application header - on every screen, in every screen
 * test - and a Radix popper opening under jsdom is measured in tens of seconds
 * (see the note on `qc-templates-page.test.tsx`). A three-button row costs
 * nothing to render and nothing to test.
 *
 * `radiogroup` and not a set of buttons or a `<select>`: a radio group is
 * exactly "one of these three, one is current", which is what a screen reader
 * should hear, and arrow-key navigation between the options comes with it.
 */

import { Monitor, Moon, Sun } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import { useTheme } from '@/app/theme-context'
import { THEME_PREFERENCES } from '@/lib/theme'
import type { ThemePreference } from '@/lib/theme'
import { cn } from '@/lib/utils'

const OPTION: Record<ThemePreference, { label: string; hint: string; Icon: LucideIcon }> = {
  light: { label: 'Light', hint: 'Always use the light theme', Icon: Sun },
  dark: { label: 'Dark', hint: 'Always use the dark theme', Icon: Moon },
  system: { label: 'System', hint: 'Match the operating system', Icon: Monitor },
}

export interface ThemeToggleProps {
  /**
   * Show the option names beside their icons. Off by default: the header is
   * tight, and the accessible name carries the same information there.
   */
  showLabels?: boolean
  className?: string
}

export function ThemeToggle({ showLabels = false, className }: ThemeToggleProps) {
  const { preference, setPreference } = useTheme()

  return (
    <div
      role="radiogroup"
      aria-label="Theme"
      className={cn(
        'inline-flex shrink items-center gap-0.5 rounded-md border border-input bg-background p-0.5',
        className,
      )}
    >
      {THEME_PREFERENCES.map((option) => {
        const { label, hint, Icon } = OPTION[option]
        const selected = preference === option
        return (
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={selected}
            aria-label={label}
            title={hint}
            onClick={() => setPreference(option)}
            className={cn(
              'inline-flex h-7 items-center justify-center gap-1.5 rounded px-2 text-xs font-medium transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              selected
                ? 'bg-accent text-accent-foreground'
                : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground',
            )}
          >
            <Icon className="h-3.5 w-3.5" aria-hidden="true" />
            {showLabels && <span>{label}</span>}
          </button>
        )
      })}
    </div>
  )
}

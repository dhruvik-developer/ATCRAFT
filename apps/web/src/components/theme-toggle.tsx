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
 * A radio group and not a set of buttons or a `<select>`: it is exactly "one of
 * these three, one is current", which is what a screen reader should hear.
 * Each option is a native radio input, visually hidden inside its label, so the
 * browser supplies the radio behaviour - one Tab stop for the group, landing on
 * the current option, and the arrow keys to change it. ARIA radio roles on
 * buttons, which this was before, give the announcement and none of that: the
 * group cost three Tab stops and the arrows did nothing.
 */

import { Monitor, Moon, Sun } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useId } from 'react'

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
  // The header, a portal header and the sign-in page can each hold one, so the
  // group name is per instance rather than a shared literal.
  const name = useId()

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
          <label
            key={option}
            title={hint}
            className={cn(
              'inline-flex h-7 cursor-pointer items-center justify-center gap-1.5 rounded px-2 text-xs font-medium transition-colors',
              // The input is visually hidden, so its label shows the focus.
              'has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring',
              selected
                ? 'bg-accent text-accent-foreground'
                : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground',
            )}
          >
            <input
              type="radio"
              name={name}
              value={option}
              checked={selected}
              onChange={() => setPreference(option)}
              aria-label={label}
              title={hint}
              className="sr-only"
            />
            <Icon className="h-3.5 w-3.5" aria-hidden="true" />
            {showLabels && <span>{label}</span>}
          </label>
        )
      })}
    </div>
  )
}

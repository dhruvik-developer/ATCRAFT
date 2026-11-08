import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { cn } from '@/lib/utils'

import { Button, buttonVariants } from './button'

describe('the destructive-outline variant', () => {
  it('stays destructive under the pointer', () => {
    const classes = buttonVariants({ variant: 'destructive-outline' }).split(/\s+/)

    expect(classes).toContain('text-destructive')
    expect(classes).toContain('hover:text-destructive')
    // The defect it replaces: `outline` + `className="text-destructive"` kept
    // the outline's hover colour, which beat the red the moment it mattered.
    expect(classes.some((name) => name.startsWith('hover:text-accent'))).toBe(false)
  })

  it('keeps a caller layout class without reintroducing a hover colour', () => {
    render(
      <Button variant="destructive-outline" className="mr-auto">
        Reject
      </Button>,
    )
    const classes = screen.getByRole('button', { name: 'Reject' }).className.split(/\s+/)

    expect(classes).toContain('mr-auto')
    expect(classes).toContain('text-destructive')
    expect(classes.some((name) => name.startsWith('hover:text-accent'))).toBe(false)
  })

  it('is what the old spelling never was', () => {
    // Pins the reason the variant exists, so nobody "simplifies" it back. This
    // is the class list `<Button variant="outline" className="text-destructive">`
    // rendered - spelled through the function because the JSX form is now a
    // lint error.
    const classes = cn(buttonVariants({ variant: 'outline', className: 'text-destructive' })).split(
      /\s+/,
    )

    expect(classes).toContain('text-destructive')
    expect(classes).toContain('hover:text-accent-foreground')
  })
})

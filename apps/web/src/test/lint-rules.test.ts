/**
 * The lint rules that stand in for a review comment, fed the code they forbid.
 *
 * A rule whose selector has a typo reports nothing, and a lint run that reports
 * nothing looks exactly like clean code - so each rule here is proven to fire on
 * its violation and to stay quiet on the sanctioned spelling. The config is the
 * real `eslint.config.js`, loaded the way `npm run lint` loads it.
 */

import { ESLint } from 'eslint'
import { describe, expect, it } from 'vitest'

const eslint = new ESLint()

/** Rule ids reported for `code`, linted as if it were a component under src/. */
async function rulesFiredBy(code: string): Promise<string[]> {
  const [result] = await eslint.lintText(code, { filePath: 'src/lint-rule-fixture.tsx' })
  return (result?.messages ?? []).map((message) => message.ruleId ?? 'parse-error')
}

describe('the lint rules that replace review comments', () => {
  it('refuses the native alert, confirm and prompt dialogs', async () => {
    const fired = await rulesFiredBy(
      [
        'export function cancel(): boolean {',
        "  window.alert('Cancelled')",
        "  const reason = window.prompt('Why?', 'Customer requested cancellation')",
        "  return confirm('Sure?') && reason !== null",
        '}',
        '',
      ].join('\n'),
    )

    expect(fired.filter((rule) => rule === 'no-alert')).toHaveLength(3)
  })

  it('refuses a Button coloured destructive through className', async () => {
    const fired = await rulesFiredBy(
      [
        "import { Button } from '@/components/ui/button'",
        '',
        'export function Actions() {',
        '  return (',
        '    <>',
        '      <Button variant="outline" className="text-destructive">Reject</Button>',
        '      <Button variant="ghost" size="sm" className="mr-auto text-destructive">Cancel</Button>',
        '    </>',
        '  )',
        '}',
        '',
      ].join('\n'),
    )

    expect(fired.filter((rule) => rule === 'no-restricted-syntax')).toHaveLength(2)
  })

  it('accepts the variant, and a hover-only or foreground class', async () => {
    const fired = await rulesFiredBy(
      [
        "import { Button } from '@/components/ui/button'",
        '',
        'export function Actions() {',
        '  return (',
        '    <>',
        '      <Button variant="destructive-outline" className="mr-auto">Reject</Button>',
        '      <Button variant="ghost" className="hover:text-destructive">Remove</Button>',
        '      <span className="text-destructive">Overdue</span>',
        '    </>',
        '  )',
        '}',
        '',
      ].join('\n'),
    )

    expect(fired).toEqual([])
  })
})

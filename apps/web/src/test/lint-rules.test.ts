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
async function rulesFiredBy(
  code: string,
  filePath = 'src/lint-rule-fixture.tsx',
): Promise<string[]> {
  const [result] = await eslint.lintText(code, { filePath })
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

  it('refuses a slice of toISOString, which is the UTC date or time', async () => {
    const fired = await rulesFiredBy(
      [
        'export function stamps(shippedAt: Date) {',
        '  const today = new Date().toISOString().slice(0, 10)',
        '  const shipped = shippedAt.toISOString().substring(0, 10)',
        '  const clock = new Date(Date.now()).toISOString().substr(11, 5)',
        '  return [today, shipped, clock]',
        '}',
        '',
      ].join('\n'),
    )

    expect(fired.filter((rule) => rule === 'no-restricted-syntax')).toHaveLength(3)
  })

  it('accepts the local-date helpers and a whole instant', async () => {
    const fired = await rulesFiredBy(
      [
        "import { toDateInput, toDateTimeInput } from '@/lib/format'",
        '',
        'export function stamps(shippedAt: Date) {',
        '  const today = toDateInput(new Date())',
        '  const local = toDateTimeInput(shippedAt)',
        '  const instant = shippedAt.toISOString()',
        "  const code = 'INV-2026-0001'.slice(0, 3)",
        '  return [today, local, instant, code]',
        '}',
        '',
      ].join('\n'),
    )

    expect(fired).toEqual([])
  })

  it('lets lib/format.ts keep its UTC calendar arithmetic', async () => {
    const code = [
      'export function addDaysISO(iso: string, delta: number): string {',
      '  const date = new Date(`${iso}T00:00:00Z`)',
      '  date.setUTCDate(date.getUTCDate() + delta)',
      '  return date.toISOString().slice(0, 10)',
      '}',
      '',
    ].join('\n')

    expect(await rulesFiredBy(code, 'src/lib/format.ts')).toEqual([])
    expect(await rulesFiredBy(code, 'src/features/production/planning-model.ts')).toEqual([
      'no-restricted-syntax',
    ])
  })

  it('refuses Number() or parseFloat() over an API decimal', async () => {
    const fired = await rulesFiredBy(
      [
        'type Line = { quantity: string; credited_quantity: string; unit_price: string }',
        'export function remaining(line: Line): number {',
        '  return Number(line.quantity) - Number(line.credited_quantity) + parseFloat(line.unit_price)',
        '}',
        '',
      ].join('\n'),
    )

    expect(fired.filter((rule) => rule === 'no-restricted-syntax')).toHaveLength(3)
  })

  it('accepts decimal arithmetic, and Number() over a plain count', async () => {
    const fired = await rulesFiredBy(
      [
        "import { toDecimal } from '@/lib/format'",
        '',
        'type Line = { quantity: string; credited_quantity: string; packages: string }',
        'export function remaining(line: Line) {',
        '  const left = toDecimal(line.quantity).minus(toDecimal(line.credited_quantity))',
        '  return [left.toFixed(), Number(line.packages)]',
        '}',
        '',
      ].join('\n'),
    )

    expect(fired).toEqual([])
  })

  it('refuses the locale methods, which format in the browser locale', async () => {
    const code = [
      'export function labels(total: number, at: Date) {',
      '  return [total.toLocaleString(), at.toLocaleDateString(), at.toLocaleTimeString()]',
      '}',
      '',
    ].join('\n')

    expect(await rulesFiredBy(code)).toEqual([
      'no-restricted-properties',
      'no-restricted-properties',
      'no-restricted-properties',
    ])
    expect(await rulesFiredBy(code, 'src/lib/format.ts')).toEqual([])
  })

  it('refuses a second QueryClient anywhere but lib/query-client.ts', async () => {
    const code = [
      "import { QueryClient } from '@tanstack/react-query'",
      '',
      'export const client = new QueryClient({ defaultOptions: { queries: { gcTime: 0 } } })',
      '',
    ].join('\n')

    expect(await rulesFiredBy(code)).toEqual(['no-restricted-syntax'])
    expect(await rulesFiredBy(code, 'src/test/harness.tsx')).toEqual(['no-restricted-syntax'])
    expect(await rulesFiredBy(code, 'src/features/items/items-page.test.tsx')).toEqual([
      'no-restricted-syntax',
    ])
    expect(await rulesFiredBy(code, 'src/lib/query-client.ts')).toEqual([])
  })

  it('accepts a client built by createQueryClient', async () => {
    const fired = await rulesFiredBy(
      [
        "import { createQueryClient } from '@/lib/query-client'",
        '',
        'export const client = createQueryClient({ defaultOptions: { queries: { retry: false } } })',
        '',
      ].join('\n'),
    )

    expect(fired).toEqual([])
  })

  it('keeps the other syntax rules in force inside lib/query-client.ts', async () => {
    const fired = await rulesFiredBy(
      'export const today = (): string => new Date().toISOString().slice(0, 10)\n',
      'src/lib/query-client.ts',
    )

    expect(fired).toEqual(['no-restricted-syntax'])
  })

  it('refuses a query key rooted in a literal, in every position a key is written', async () => {
    const code = [
      "import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'",
      "import { useListQuery } from '@/hooks/use-list-query'",
      "import { OptionsPicker } from '@/components/options-picker'",
      '',
      "export const dealerKeys = { all: ['dealers'] as const, list: (p: unknown) => ['dealers', 'list', p] as const }",
      "const pendingKey = (p: unknown) => ['quality', 'pending', p] as const",
      "const CHANNELS_QUERY_KEY = ['notifications', 'channel-templates'] as const",
      '',
      'export function Screen({ id, kind }: { id: string; kind: string }) {',
      '  const client = useQueryClient()',
      "  useQuery({ queryKey: ['finance', 'credit-notes', 'detail', id], queryFn: () => id })",
      "  useQuery({ queryKey: ['quality', 'raise-templates', id] as const, queryFn: () => id })",
      "  useListQuery({ path: '/promotions', queryKey: (p) => ['promotions', kind, p] })",
      '  useMutation({ mutationKey: [`packing`, id], mutationFn: async () => id })',
      "  void client.invalidateQueries({ queryKey: ['finance'] })",
      "  client.setQueryData(['inventory', 'balances'], [])",
      "  return <OptionsPicker queryKey={(p: unknown) => ['masters', 'uoms', p]} />",
      '}',
      '',
    ].join('\n')

    const fired = await rulesFiredBy(code)

    // Two in the factory, one each for the other nine.
    expect(fired.filter((rule) => rule === 'no-restricted-syntax')).toHaveLength(11)
  })

  it('accepts a key from the registry, or a factory spreading one of its roots', async () => {
    const fired = await rulesFiredBy(
      [
        "import { useQuery, useQueryClient } from '@tanstack/react-query'",
        "import { useListQuery } from '@/hooks/use-list-query'",
        "import { queryKeys } from '@/lib/query-client'",
        '',
        'const dealerKeys = {',
        "  all: [...queryKeys.customers.all, 'dealers'] as const,",
        "  list: (p: unknown) => [...queryKeys.customers.all, 'dealers', 'list', p] as const,",
        '}',
        "const STATUSES = ['DRAFT', 'POSTED'] as const",
        "const filterKeys = [...STATUSES, 'vendor_id']",
        '',
        'export function Screen({ id }: { id: string }) {',
        '  const client = useQueryClient()',
        '  useQuery({ queryKey: queryKeys.customers.detail(id), queryFn: () => id })',
        "  useQuery({ queryKey: [...queryKeys.inventory.all, 'serials', id] as const, queryFn: () => id })",
        "  useListQuery({ path: '/dealers', queryKey: dealerKeys.list })",
        "  client.setQueryData(queryKeys.sales.settings, ['a', 'b'])",
        '  return filterKeys.length',
        '}',
        '',
      ].join('\n'),
    )

    expect(fired).toEqual([])
  })

  it('lets the registry spell its roots, and a test spell a probe key', async () => {
    const code = [
      "export const queryKeys = { finance: { all: ['finance'] as const } } as const",
      "const probeKey = ['harness', 'probe'] as const",
      'export const keys = [queryKeys, probeKey]',
      '',
    ].join('\n')

    expect(await rulesFiredBy(code, 'src/lib/query-client.ts')).toEqual([])
    expect(await rulesFiredBy(code, 'src/test/harness.test.tsx')).toEqual([])
    // Elsewhere both are roots the registry does not hold.
    expect(await rulesFiredBy(code, 'src/features/finance/finance-types.ts')).toEqual([
      'no-restricted-syntax',
      'no-restricted-syntax',
    ])
  })

  it('refuses bare browser storage outside lib/storage.ts and the tests', async () => {
    const code = [
      'export function remember(collapsed: boolean): boolean {',
      "  localStorage.setItem('atcraft.sidebar', String(collapsed))",
      "  window.sessionStorage.removeItem('atcraft.chunk-reloaded-at')",
      "  return sessionStorage.getItem('atcraft.theme') === 'dark'",
      '}',
      '',
    ].join('\n')

    expect(await rulesFiredBy(code)).toEqual([
      'no-restricted-globals',
      'no-restricted-properties',
      'no-restricted-globals',
    ])
    expect(await rulesFiredBy(code, 'src/lib/storage.ts')).toEqual([])
    expect(await rulesFiredBy(code, 'src/app/app-layout.test.tsx')).toEqual([])
  })
})

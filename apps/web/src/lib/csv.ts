/**
 * CSV the browser writes itself, safe to open in a spreadsheet.
 *
 * Most exports are rendered by the server, whose XLSX writer passes every text
 * cell through `_neutralise_formula` (apps/api/src/workers/tasks/reports.py).
 * A client-side CSV skipped that: the job-work vendor ledger wrote item codes
 * verbatim, and an item code is only normalised, so one beginning with `=` is
 * valid - and runs as a formula, or leaks data through a link, for the finance
 * user who opens the file in Excel or LibreOffice. That person is rarely the
 * one who typed the code, and usually has more authority.
 *
 * `csvCell` applies the same remedy as the server: a text value that begins with
 * a formula lead gets a leading apostrophe, which spreadsheets drop on display
 * and read as "this is text". A plain number such as `-5.0000` is a number, not
 * a formula, and passes unchanged. Every CSV the browser builds goes through
 * `downloadCsv`, which a frontend guard holds to.
 */

/**
 * What makes Excel and LibreOffice read a text cell as a formula. `@` starts a
 * legacy function call; a leading tab or carriage return is skipped by the
 * parser before the first significant character is examined.
 */
const FORMULA_LEADS = ['=', '+', '-', '@', '\t', '\r'] as const

/** A plain decimal, which a leading sign does not turn into a formula. */
const PLAIN_NUMBER = /^[+-]?\d+(\.\d+)?$/

/** The characters that oblige a CSV field to be quoted. */
const NEEDS_QUOTES = /[",\r\n]/

export type CsvValue = string | number | null | undefined

/** One field: neutralised if it could run as a formula, then quoted if it must be. */
export function csvCell(value: CsvValue): string {
  const text = value === null || value === undefined ? '' : String(value)
  const inert =
    !PLAIN_NUMBER.test(text) && FORMULA_LEADS.some((lead) => text.startsWith(lead))
      ? `'${text}`
      : text
  return NEEDS_QUOTES.test(inert) ? `"${inert.replace(/"/g, '""')}"` : inert
}

/** Rows of fields as CSV text, one line per row. */
export function csvText(rows: readonly (readonly CsvValue[])[]): string {
  return rows.map((row) => row.map(csvCell).join(',')).join('\n')
}

/** Save rows as a CSV file the browser downloads; the first row is the header. */
export function downloadCsv(filename: string, rows: readonly (readonly CsvValue[])[]): void {
  const blob = new Blob([csvText(rows)], { type: 'text/csv' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

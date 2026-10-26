import { useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Badge, Input, Label } from '@/components/ui/primitives'
import { humanise } from '@/lib/format'

import {
  isFinalSerialDisposition,
  SERIAL_DISPOSITIONS,
  type SerialDisposition,
} from './serial-dispositions'

const PAGE_SIZE = 50

/** The unit set belongs to this inspection; only its bounded form rows are paged here. */
export function SerialDispositionEditor({
  ids,
  labels,
  values,
  previous,
  allowScrap,
  readOnly = false,
  onChange,
}: {
  ids: readonly string[]
  labels: Readonly<Record<string, string>>
  values: Readonly<Record<string, SerialDisposition>>
  previous: Readonly<Record<string, string>>
  allowScrap: boolean
  readOnly?: boolean
  onChange: (values: Record<string, SerialDisposition>) => void
}) {
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const matches = useMemo(() => {
    const term = search.trim().toLocaleLowerCase()
    return ids.filter((id) => !term || (labels[id] || id).toLocaleLowerCase().includes(term))
  }, [ids, labels, search])
  const rows = matches.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)
  return (
    <section className="space-y-3 rounded-md border p-3">
      <h4 className="text-sm font-semibold">
        {readOnly ? 'Serial unit outcomes' : 'Disposition each serial unit'}
      </h4>
      <p className="text-xs text-muted-foreground">
        {readOnly
          ? 'Recorded outcomes for each physical unit.'
          : 'Choose the actual outcome for each label. Units already accepted, rejected or scrapped remain fixed; only held or rework units can change.'}
      </p>
      <div>
        <Label htmlFor="qc-unit-search">Find serial label</Label>
        <Input
          id="qc-unit-search"
          value={search}
          onChange={(event) => {
            setSearch(event.target.value)
            setPage(1)
          }}
          className="mt-1"
        />
      </div>
      <div className="space-y-2">
        {rows.map((id) => {
          const label = labels[id] || id
          const locked = isFinalSerialDisposition(previous[id])
          return (
            <div key={id} className="grid items-center gap-2 rounded border p-2 sm:grid-cols-2">
              <div className="min-w-0">
                <Label htmlFor={`qc-unit-${id}`} className="break-all font-mono text-xs">
                  {label}
                </Label>
                {locked && (
                  <Badge variant="muted" className="ml-2">
                    Final
                  </Badge>
                )}
              </div>
              <select
                id={`qc-unit-${id}`}
                aria-label={`Disposition for ${label}`}
                value={values[id] || ''}
                disabled={readOnly || locked}
                className="min-h-11 w-full rounded-md border bg-background px-3 text-sm"
                onChange={(event) => {
                  const next = { ...values }
                  if (event.target.value) next[id] = event.target.value as SerialDisposition
                  else delete next[id]
                  onChange(next)
                }}
              >
                <option value="">Choose disposition</option>
                {SERIAL_DISPOSITIONS.filter(
                  (choice) => choice !== 'SCRAP' || allowScrap || values[id] === 'SCRAP',
                ).map((choice) => (
                  <option key={choice} value={choice}>
                    {humanise(choice)}
                  </option>
                ))}
              </select>
            </div>
          )
        })}
        {!rows.length && (
          <p className="text-sm text-muted-foreground">No serial labels match this search.</p>
        )}
      </div>
      <div className="flex items-center justify-between gap-3">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={page <= 1}
          onClick={() => setPage(page - 1)}
        >
          Previous units
        </Button>
        <span className="text-xs text-muted-foreground">
          {Object.keys(values).length} of {ids.length} identified
        </span>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={page * PAGE_SIZE >= matches.length}
          onClick={() => setPage(page + 1)}
        >
          Next units
        </Button>
      </div>
    </section>
  )
}

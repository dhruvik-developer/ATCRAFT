import { useMutation, useQueryClient } from '@tanstack/react-query'
import { addDays, format, parseISO } from 'date-fns'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/overlays'
import { Alert, AlertDescription, Input, Label, Textarea } from '@/components/ui/primitives'
import { useOperatorOptions } from '@/hooks/use-operator-options'
import { shiftLabel, useShiftOptions } from '@/hooks/use-shift-options'
import { api, ApiError } from '@/lib/api-client'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

export type RosterCell = {
  work_center_id: string
  machine_id: string | null
  shift_id: string
  roster_date: string
}
export function RosterDialog({
  mode,
  day,
  cell,
  centres,
  machines,
  onClose,
}: {
  mode: 'assign' | 'bulk' | 'copy'
  day: string
  cell?: RosterCell
  centres: SchemaOf<'WorkCenterRead'>[]
  machines: SchemaOf<'MachineRead'>[]
  onClose: () => void
}) {
  const cache = useQueryClient()
  const shifts = useShiftOptions()
  const operators = useOperatorOptions()
  const [date, setDate] = useState(cell?.roster_date ?? day)
  const [centre, setCentre] = useState(cell?.work_center_id ?? '')
  const [machine, setMachine] = useState(cell?.machine_id ?? '')
  const [shift, setShift] = useState(cell?.shift_id ?? '')
  const [people, setPeople] = useState<string[]>([])
  const [notes, setNotes] = useState('')
  const tomorrow = format(addDays(parseISO(day), 1), 'yyyy-MM-dd')
  const [from, setFrom] = useState(tomorrow)
  const [to, setTo] = useState(tomorrow)
  const save = useMutation({
    mutationFn: () => {
      if (mode === 'copy')
        return api.post('/routing/roster/assignments/copy', {
          source_date: date,
          target_from: from,
          target_to: to,
          work_center_id: centre || null,
        } satisfies SchemaOf<'ShiftRosterCopyRequest'>)
      const assignments = people.map(
        (operator_user_id) =>
          ({
            roster_date: date,
            shift_id: shift,
            work_center_id: centre,
            machine_id: machine || null,
            operator_user_id,
            notes: notes || null,
          }) satisfies SchemaOf<'ShiftAssignmentCreate'>,
      )
      return mode === 'bulk'
        ? api.post('/routing/roster/assignments/bulk', { assignments })
        : api.post('/routing/roster/assignments', assignments[0])
    },
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: queryKeys.routing.all })
      toast.success('Assignments saved')
      onClose()
    },
  })
  const conflicts =
    save.error instanceof ApiError && Array.isArray(save.error.details?.conflicts)
      ? save.error.details.conflicts.filter(
          (row): row is Record<string, unknown> => typeof row === 'object' && row !== null,
        )
      : []
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {mode === 'copy' ? 'Copy day' : mode === 'bulk' ? 'Bulk assign' : 'Assign operator'}
          </DialogTitle>
          <DialogDescription>
            One operator per machine. Cell assignments can cover several machines. Overlaps are
            refused together.
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            save.mutate()
          }}
        >
          <DialogBody className="space-y-4">
            {save.error && (
              <Alert variant="destructive">
                <AlertDescription>
                  {save.error.message}
                  {conflicts.length > 0 && (
                    <ul className="mt-2 list-disc pl-4">
                      {conflicts.map((row, index) => (
                        <li key={index}>
                          {typeof row.message === 'string'
                            ? row.message
                            : `${String(row.resource)}: ${String(row.operator_name ?? row.operator_user_id)}, ${String(row.roster_date)}, ${String(row.shift_code ?? row.shift_id)}, ${String(row.work_center_code ?? row.work_center_id)}`}
                        </li>
                      ))}
                    </ul>
                  )}
                </AlertDescription>
              </Alert>
            )}
            <div>
              <Label htmlFor="roster-date">{mode === 'copy' ? 'Source date' : 'Date'}</Label>
              <Input
                id="roster-date"
                type="date"
                value={date}
                onChange={(event) => setDate(event.target.value)}
                required
              />
            </div>
            <div>
              <Label htmlFor="roster-centre">Work centre</Label>
              <select
                id="roster-centre"
                className="h-10 w-full rounded-md border bg-background px-3"
                value={centre}
                onChange={(event) => {
                  setCentre(event.target.value)
                  setMachine('')
                }}
                required={mode !== 'copy'}
              >
                <option value="">
                  {mode === 'copy' ? 'All work centres' : 'Choose a work centre'}
                </option>
                {centres.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.code} · {row.name}
                  </option>
                ))}
              </select>
            </div>
            {mode === 'copy' ? (
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <Label htmlFor="copy-from">Target from</Label>
                  <Input
                    id="copy-from"
                    type="date"
                    value={from}
                    onChange={(event) => setFrom(event.target.value)}
                    required
                  />
                </div>
                <div>
                  <Label htmlFor="copy-to">Target to</Label>
                  <Input
                    id="copy-to"
                    type="date"
                    value={to}
                    onChange={(event) => setTo(event.target.value)}
                    required
                  />
                </div>
              </div>
            ) : (
              <>
                <div>
                  <Label htmlFor="roster-shift">Shift</Label>
                  <select
                    id="roster-shift"
                    className="h-10 w-full rounded-md border bg-background px-3"
                    value={shift}
                    onChange={(event) => setShift(event.target.value)}
                    required
                  >
                    <option value="">Choose a shift</option>
                    {(shifts.data ?? []).map((row) => (
                      <option key={row.id} value={row.id}>
                        {shiftLabel(row)}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <Label htmlFor="roster-machine">Machine</Label>
                  <select
                    id="roster-machine"
                    className="h-10 w-full rounded-md border bg-background px-3"
                    value={machine}
                    onChange={(event) => setMachine(event.target.value)}
                  >
                    <option value="">Cell level</option>
                    {machines
                      .filter(
                        (row) =>
                          row.work_center_id === centre &&
                          row.is_active &&
                          row.status !== 'RETIRED',
                      )
                      .map((row) => (
                        <option key={row.id} value={row.id}>
                          {row.code} · {row.name}
                        </option>
                      ))}
                  </select>
                </div>
                {machines.some(
                  (row) =>
                    row.id === machine &&
                    (row.status === 'MAINTENANCE' || row.status === 'BREAKDOWN'),
                ) && (
                  <Alert>
                    <AlertDescription>
                      This machine is under maintenance or in breakdown. The assignment is saved
                      with a warning.
                    </AlertDescription>
                  </Alert>
                )}
                <div>
                  <Label htmlFor="roster-operator">Operator</Label>
                  <select
                    id="roster-operator"
                    className="min-h-10 w-full rounded-md border bg-background px-3"
                    multiple={mode === 'bulk'}
                    value={mode === 'bulk' ? people : (people[0] ?? '')}
                    onChange={(event) =>
                      setPeople(
                        Array.from(event.target.selectedOptions)
                          .map((option) => option.value)
                          .filter(Boolean),
                      )
                    }
                    required
                  >
                    <option value="" disabled>
                      Choose an operator
                    </option>
                    {(operators.data ?? []).map((row) => (
                      <option key={row.id} value={row.id}>
                        {row.full_name} {row.employee_code ?? ''}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <Label htmlFor="roster-notes">Notes</Label>
                  <Textarea
                    id="roster-notes"
                    value={notes}
                    onChange={(event) => setNotes(event.target.value)}
                    maxLength={2000}
                  />
                </div>
              </>
            )}
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={save.isPending || (mode !== 'copy' && people.length === 0)}
            >
              {mode === 'copy' ? 'Copy assignments' : 'Save assignments'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

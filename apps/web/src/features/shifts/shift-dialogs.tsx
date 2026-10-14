import { useMutation, useQueryClient } from '@tanstack/react-query'
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
import { Alert, AlertDescription, Input, Label } from '@/components/ui/primitives'
import type { ShiftOption } from '@/hooks/use-shift-options'
import { api } from '@/lib/api-client'
import { success } from '@/lib/messages'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'
import type { SchemaOf } from '@/types/api'

export function ShiftFormDialog({
  shift,
  onClose,
}: {
  shift: ShiftOption | null
  onClose: () => void
}) {
  const [code, setCode] = useState(shift?.code ?? '')
  const [name, setName] = useState(shift?.name ?? '')
  const [start, setStart] = useState(shift?.start_time.slice(0, 5) ?? '06:00')
  const [end, setEnd] = useState(shift?.end_time.slice(0, 5) ?? '14:00')
  const [pause, setPause] = useState(shift?.break_minutes ?? 0)
  const [order, setOrder] = useState(shift?.sort_order ?? 0)
  const cache = useQueryClient()
  const save = useMutation({
    mutationFn: () => {
      const payload = {
        name,
        start_time: start,
        end_time: end,
        break_minutes: pause,
        sort_order: order,
      }
      return shift
        ? api.patch(`/routing/shifts/${shift.id}`, payload satisfies SchemaOf<'ShiftUpdate'>)
        : api.post('/routing/shifts', {
            ...payload,
            code,
            is_active: true,
          } satisfies SchemaOf<'ShiftCreate'>)
    },
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: queryKeys.routing.all })
      toast.success(shift ? success.updated('Shift') : success.created('Shift'))
      onClose()
    },
  })
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{shift ? 'Edit shift' : 'New shift'}</DialogTitle>
          <DialogDescription>
            Local times in your company timezone. Codes identify historical bookings.
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
                <AlertDescription>{save.error.message}</AlertDescription>
              </Alert>
            )}
            <div>
              <Label htmlFor="shift-code">Code</Label>
              <Input
                id="shift-code"
                value={code}
                onChange={(event) => setCode(event.target.value)}
                maxLength={16}
                required
                disabled={Boolean(shift)}
              />
            </div>
            <div>
              <Label htmlFor="shift-name">Name</Label>
              <Input
                id="shift-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                maxLength={64}
                required
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <Label htmlFor="shift-start">Start time</Label>
                <Input
                  id="shift-start"
                  type="time"
                  value={start}
                  onChange={(event) => setStart(event.target.value)}
                  required
                />
              </div>
              <div>
                <Label htmlFor="shift-end">End time</Label>
                <Input
                  id="shift-end"
                  type="time"
                  value={end}
                  onChange={(event) => setEnd(event.target.value)}
                  required
                />
              </div>
            </div>
            <p className="text-sm text-muted-foreground">
              {end <= start
                ? 'Crosses midnight; ends the following day.'
                : 'Starts and ends on the same day.'}
            </p>
            <div>
              <Label htmlFor="shift-break">Break minutes</Label>
              <Input
                id="shift-break"
                type="number"
                min={0}
                max={1439}
                value={pause}
                onChange={(event) => setPause(Number(event.target.value))}
                required
              />
            </div>
            <div>
              <Label htmlFor="shift-sort">Sort order</Label>
              <Input
                id="shift-sort"
                type="number"
                min={0}
                max={9999}
                value={order}
                onChange={(event) => setOrder(Number(event.target.value))}
                required
              />
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending}>
              Save shift
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

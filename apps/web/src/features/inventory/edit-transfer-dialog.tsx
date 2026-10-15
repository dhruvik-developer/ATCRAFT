import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { FieldError } from '@/components/shared'
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
import { Input, Label, Textarea } from '@/components/ui/primitives'
import { api } from '@/lib/api-client'
import { REASON_MIN_LENGTH } from '@/lib/constants'
import { toDateInput } from '@/lib/format'
import { success } from '@/lib/messages'
import { queryKeys } from '@/lib/query-client'
import { toast } from '@/lib/toast'

import type { StockTransferRow } from './types'

export default function EditTransferDialog({
  transfer,
  onClose,
}: {
  transfer: StockTransferRow
  onClose: () => void
}) {
  const client = useQueryClient()
  const [reason, setReason] = useState(transfer.reason)
  const [expected, setExpected] = useState(toDateInput(transfer.expected_date))
  const [notes, setNotes] = useState(transfer.notes ?? '')
  const update = useMutation({
    mutationFn: () =>
      api.patch<StockTransferRow>(`/inventory/transfers/${transfer.id}`, {
        reason: reason.trim(),
        expected_date: expected || null,
        notes: notes.trim() || null,
      }),
    onSuccess: () => {
      toast.success(success.updated('Transfer'), transfer.code)
      void client.invalidateQueries({ queryKey: queryKeys.inventory.all })
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
          <DialogTitle>Edit {transfer.code}</DialogTitle>
          <DialogDescription>
            Update the reason, expected date and notes before this transfer is approved.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="transfer-edit-reason" required>
              Reason
            </Label>
            <Textarea
              id="transfer-edit-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="transfer-edit-expected">Expected date</Label>
            <Input
              id="transfer-edit-expected"
              type="date"
              value={expected}
              onChange={(event) => setExpected(event.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="transfer-edit-notes">Notes</Label>
            <Textarea
              id="transfer-edit-notes"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
            />
          </div>
          <FieldError message={update.error?.message} />
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" disabled={update.isPending} onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={update.isPending}
            disabled={reason.trim().length < REASON_MIN_LENGTH}
            onClick={() => update.mutate()}
          >
            Save changes
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

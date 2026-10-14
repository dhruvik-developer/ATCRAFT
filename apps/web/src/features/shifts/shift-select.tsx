import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/overlays'
import { shiftLabel, useShiftOptions } from '@/hooks/use-shift-options'
import { SELECT_NONE } from '@/lib/constants'

export function ShiftSelect({
  id,
  value,
  onChange,
  enabled = true,
  legacy,
}: {
  id: string
  value: string | null
  onChange: (value: string | null) => void
  enabled?: boolean
  legacy?: string | null
}) {
  const shifts = useShiftOptions(enabled)
  return (
    <>
      <Select
        value={value ?? SELECT_NONE}
        onValueChange={(next) => onChange(next === SELECT_NONE ? null : next)}
      >
        <SelectTrigger id={id}>
          <SelectValue placeholder="Choose a shift" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={SELECT_NONE}>Unassigned</SelectItem>
          {value && !shifts.data?.some((row) => row.id === value) && (
            <SelectItem value={value}>{legacy ?? value}</SelectItem>
          )}
          {(shifts.data ?? []).map((shift) => (
            <SelectItem key={shift.id} value={shift.id}>
              {shiftLabel(shift)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {!value && legacy && <p className="text-xs text-muted-foreground">Legacy shift: {legacy}</p>}
    </>
  )
}

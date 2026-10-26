import type { SchemaOf } from '@/types/api'

export type SerialDisposition = NonNullable<
  SchemaOf<'DecideRequest'>['serial_dispositions']
>[string]
export const SERIAL_DISPOSITIONS = [
  'ACCEPTED',
  'REJECTED',
  'REWORK',
  'HOLD',
  'SCRAP',
] as const satisfies readonly SerialDisposition[]

const QUANTITY_FIELD = {
  ACCEPTED: 'accepted_quantity',
  REJECTED: 'rejected_quantity',
  REWORK: 'rework_quantity',
  HOLD: 'hold_quantity',
  SCRAP: 'scrap_quantity',
} as const

/** Integer unit counts become exact decimal strings; quantities are never rounded. */
export function serialDispositionQuantities(
  dispositions: Readonly<Record<string, SerialDisposition>>,
) {
  const counts = {
    accepted_quantity: 0,
    rejected_quantity: 0,
    rework_quantity: 0,
    hold_quantity: 0,
    scrap_quantity: 0,
  }
  for (const value of Object.values(dispositions)) counts[QUANTITY_FIELD[value]] += 1
  return {
    accepted_quantity: String(counts.accepted_quantity),
    rejected_quantity: String(counts.rejected_quantity),
    rework_quantity: String(counts.rework_quantity),
    hold_quantity: String(counts.hold_quantity),
    scrap_quantity: String(counts.scrap_quantity),
  }
}

export function isFinalSerialDisposition(value: string | undefined): boolean {
  return value === 'ACCEPTED' || value === 'REJECTED' || value === 'SCRAP'
}

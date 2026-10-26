import { z } from 'zod'

import { SERIAL_BULK_MAX } from '@/features/inventory/serial-numbers'

import { REJECTION_ACTIONS } from './job-work-model'

export const JOB_WORK_QUANTITY_INPUT = /^(\d{1,14}(\.\d{1,4})?)?$/

export const receiveSchema = z
  .object({
    receipt_date: z.string().min(1, 'A receipt needs a date'),
    remarks: z.string().trim().max(500, 'At most 500 characters'),
    /** Required by the API on every receipt - it is the audit reason. */
    reason: z.string().trim().min(1, 'Say what this receipt covers'),
    vendor_challan_number: z.string().trim().max(64, 'At most 64 characters'),
    /** The vendor's own delivery document is dated by them, not by us. */
    vendor_challan_date: z.string(),
    /**
     * Acknowledge loss beyond the agreed allowance. Honoured only from a caller
     * holding `job_work.close` - the excess debits the vendor ledger, so the
     * storekeeper keying the lot cannot accept it alone.
     */
    approve_excess_loss: z.boolean(),
    lines: z.array(
      z.object({
        line_number: z.number(),
        label: z.string(),
        received_quantity: z
          .string()
          .trim()
          .regex(JOB_WORK_QUANTITY_INPUT, 'Enter a quantity with up to 4 decimal places'),
        rejected_quantity: z
          .string()
          .trim()
          .regex(JOB_WORK_QUANTITY_INPUT, 'Enter a quantity with up to 4 decimal places'),
        /**
         * Material that went out and is coming back untouched. The API models
         * this as `returned_unprocessed_quantity`; there is no free-standing
         * "loss" bucket on a receipt.
         */
        returned_quantity: z
          .string()
          .trim()
          .regex(JOB_WORK_QUANTITY_INPUT, 'Enter a quantity with up to 4 decimal places'),
        /** Where rejected output physically is - ignored when nothing was rejected. */
        rejection_action: z.enum(REJECTION_ACTIONS),
        /** Why the output was rejected. Sent as the receipt line's notes. */
        rejection_reason: z.string().trim().max(500, 'At most 500 characters'),
        /**
         * Close the line: everything still at the vendor that this lot does not
         * account for is written off as process loss, within the allowance as
         * scrap and beyond it - with approval - as a vendor debit.
         */
        is_final: z.boolean(),
        output_serial_ids: z.array(z.string()).max(10_000),
        rejected_serial_ids: z.array(z.string()).max(10_000),
        returned_unprocessed_serial_ids: z.array(z.string()).max(10_000),
        scrapped_serial_ids: z.array(z.string()).max(10_000),
      }),
    ),
  })
  .superRefine((values, context) => {
    const serialCount = values.lines.reduce(
      (total, line) =>
        total +
        line.output_serial_ids.length +
        line.rejected_serial_ids.length +
        line.returned_unprocessed_serial_ids.length +
        (line.is_final ? line.scrapped_serial_ids.length : 0),
      0,
    )
    if (serialCount > SERIAL_BULK_MAX)
      context.addIssue({
        code: 'custom',
        path: ['lines'],
        message: `A receipt can account for at most ${SERIAL_BULK_MAX} serial units across all lines. Record larger returns in partial receipts.`,
      })
  })

export type ReceiveFormValues = z.infer<typeof receiveSchema>

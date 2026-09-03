/**
 * Indian statutory identifier formats.
 *
 * Validated in the browser so an obvious typo is caught before a round trip.
 * The API validates them again - this is convenience, not enforcement.
 *
 * One copy, deliberately. Customers and vendors each grew their own GSTIN
 * regular expression and the two did not agree: one accepted `0` as the
 * registration counter, the other did not. Both screens capture the same field,
 * for the same tax authority, so the looser one was simply wrong, and a second
 * definition is how that happens again.
 */

/**
 * GSTIN - 15 characters, laid out as:
 *
 * ```
 *   27  ABCDE 1234 F  1  Z  5
 *   |   |     |    |  |  |  └── checksum
 *   |   |     |    |  |  └───── fixed 'Z', reserved
 *   |   |     |    |  └──────── registration counter for this PAN in this state
 *   |   |     |    └─────────── PAN check letter
 *   |   |     └──────────────── PAN's four digits
 *   |   └────────────────────── PAN's five letters
 *   └────────────────────────── state code
 * ```
 *
 * The counter at position 13 runs 1-9 then A-Z: the first registration is `1`,
 * so a `0` there is not an identifier the department issues.
 */
export const GSTIN_PATTERN = /^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$/

/** PAN - five letters, four digits, one check letter. */
export const PAN_PATTERN = /^[A-Z]{5}[0-9]{4}[A-Z]$/

/** IFSC - four bank letters, a reserved `0`, then the six-character branch. */
export const IFSC_PATTERN = /^[A-Z]{4}0[A-Z0-9]{6}$/

/** PIN code - six digits; the first identifies a region, so it is never 0. */
export const PINCODE_PATTERN = /^[1-9][0-9]{5}$/

/** GST state code - the two digits a GSTIN opens with. */
export const STATE_CODE_PATTERN = /^[0-9]{2}$/

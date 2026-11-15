/**
 * What a decimal box accepts, stated once per column.
 *
 * A form keeps a decimal as the string that was typed and checks its shape
 * before sending, so a value its column cannot hold is refused under the field
 * rather than in a toast after a round trip. The shape is the column's: a
 * `Quantity` is `NUMERIC(18, 4)`, so 14 whole digits and 4 places. That rule had
 * been hand-written about ninety times in thirty spellings - eight with no bound
 * on the whole digits at all, and `QUANTITY_INPUT` exported three times under
 * two rules - so a change of precision touched fifty files. The bounds below
 * mirror `apps/api/src/core/types.py`; a field with a genuinely different rule
 * passes its own bounds to `decimalPattern`.
 *
 * The API stays the authority: every numeric request field is bounded there as
 * well, and its 422 still lands on the field. Its own module rather than
 * `lib/format.ts`, which the app shell loads on the first paint: only form
 * screens check what was typed, and each of those is a lazy route.
 */

/** A decimal column's shape: the digits it holds before and after the point. */
export interface DecimalBounds {
  /** Digits before the point: the column's precision less its scale. */
  readonly whole: number
  /** Digits after the point: the column's scale, or fewer where a rule keeps fewer. */
  readonly places: number
}

/** `Quantity = Numeric(18, 4)`: stock, order, BOM and booked quantities, and minutes. */
export const QUANTITY: DecimalBounds = { whole: 14, places: 4 }

/** `UnitPrice = Numeric(18, 6)`: a price or a rate per unit. */
export const UNIT_PRICE: DecimalBounds = { whole: 12, places: 6 }

/** `Money = Numeric(18, 4)`: a limit, a charge or a cost, at the column's own scale. */
export const MONEY: DecimalBounds = { whole: 14, places: 4 }

/**
 * An amount the API keeps through `to_money`, whose quantum is 0.01: a payment,
 * an allocation of one, freight. The column takes four places, so a third
 * decimal would pass a four-place rule and then be rounded away; the form asks
 * for the two that are kept.
 */
export const ROUNDED_MONEY: DecimalBounds = { whole: 14, places: 2 }

/** `Percentage = Numeric(9, 4)`, the column's own bound. */
export const PERCENTAGE: DecimalBounds = { whole: 5, places: 4 }

/** A percentage a person keys - a discount, a tax rate, a sample share - up to 999.9999. */
export const PERCENT: DecimalBounds = { whole: 3, places: 4 }

/** `ConversionFactor = Numeric(18, 8)`: how many of one unit make another. */
export const CONVERSION_FACTOR: DecimalBounds = { whole: 10, places: 8 }

/**
 * A weight in kilograms or a carton dimension, as the packing and dispatch
 * forms take it: to the gram, three places. Stored as a `Quantity`, so well
 * inside the column.
 */
export const MEASURE: DecimalBounds = { whole: 10, places: 3 }

export interface DecimalOptions {
  /** A leading minus is allowed: a variance, a signed adjustment, a delta, a reading. */
  readonly signed?: boolean
  /** An empty box passes, for a field whose blank means "not set" or zero. */
  readonly optional?: boolean
}

/**
 * The pattern a typed decimal must match, as `z.string().regex()` or `.test()`
 * takes it: `decimalPattern(QUANTITY)`, `decimalPattern(UNIT_PRICE, { optional:
 * true })`. No exponent, no leading point, no grouping commas - what a person
 * types into a box, and what the API's decimal string accepts.
 */
export function decimalPattern(
  { whole, places }: DecimalBounds,
  { signed = false, optional = false }: DecimalOptions = {},
): RegExp {
  const number = `${signed ? '-?' : ''}\\d{1,${whole}}(\\.\\d{1,${places}})?`
  return new RegExp(optional ? `^(${number})?$` : `^${number}$`)
}

/** A quantity, required. */
export const QUANTITY_INPUT = decimalPattern(QUANTITY)
/** A quantity whose blank means "not set" or zero. */
export const OPTIONAL_QUANTITY_INPUT = decimalPattern(QUANTITY, { optional: true })
/** A unit price, required. */
export const UNIT_PRICE_INPUT = decimalPattern(UNIT_PRICE)
/** A unit price whose blank means "not set" or zero. */
export const OPTIONAL_UNIT_PRICE_INPUT = decimalPattern(UNIT_PRICE, { optional: true })
/** A money amount at the column's scale, required. */
export const MONEY_INPUT = decimalPattern(MONEY)
/** A money amount at the column's scale whose blank means "not set" or zero. */
export const OPTIONAL_MONEY_INPUT = decimalPattern(MONEY, { optional: true })
/** An amount kept to two places, required. */
export const ROUNDED_MONEY_INPUT = decimalPattern(ROUNDED_MONEY)
/** A keyed percentage, required. */
export const PERCENT_INPUT = decimalPattern(PERCENT)
/** A keyed percentage whose blank means "not set" or zero. */
export const OPTIONAL_PERCENT_INPUT = decimalPattern(PERCENT, { optional: true })
/** A weight or a dimension whose blank means "not set" or zero. */
export const OPTIONAL_MEASURE_INPUT = decimalPattern(MEASURE, { optional: true })

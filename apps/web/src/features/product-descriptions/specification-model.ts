import type { QueryClient } from '@tanstack/react-query'

import { toDecimal } from '@/lib/format'
import { PRODUCT_DESCRIPTION_TEXT_MAX, SPECIFICATION_VALUE_MAX } from '@/lib/constants'
import { queryKeys } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

export type SpecificationAttribute = SchemaOf<'SpecificationAttributeRead'>
export type SpecificationValue = SchemaOf<'ItemSpecificationValueRead'>
export type SpecificationValueInput = SchemaOf<'ItemSpecificationValueIn'>
export type ItemSpecifications = SchemaOf<'ItemSpecificationsRead'>

export const SPECIFICATION_TYPES = [
  'TEXT',
  'NUMBER',
  'BOOLEAN',
  'OPTION',
] as const satisfies readonly SpecificationAttribute['value_type'][]

/** A blank draft is absent; zero and false are real recorded values. */
export function specificationDraft(value: SpecificationValue): string {
  switch (value.value_type) {
    case 'TEXT':
      return value.value_text ?? ''
    case 'NUMBER':
      return value.value_number == null ? '' : String(value.value_number)
    case 'BOOLEAN':
      return value.value_bool == null ? '' : String(value.value_bool)
    case 'OPTION':
      return value.value_option ?? ''
  }
}

export function specificationDisplay(value: SpecificationValue): string {
  if (value.value_type === 'BOOLEAN')
    return value.value_bool == null ? 'Not recorded' : value.value_bool ? 'Yes' : 'No'
  const text = specificationDraft(value)
  return text === ''
    ? 'Not recorded'
    : `${text}${value.attribute.unit ? ` ${value.attribute.unit}` : ''}`
}

/** Keep the exact decimal string on the wire; never pass a technical measure through Number. */
export function specificationInput(
  attribute: SpecificationAttribute,
  draft: string,
): SpecificationValueInput | null {
  const value = draft.trim()
  if (value === '') return null
  const base = { specification_attribute_id: attribute.id }
  switch (attribute.value_type) {
    case 'TEXT':
      if (value.length > PRODUCT_DESCRIPTION_TEXT_MAX)
        throw new Error(
          `${attribute.name}: use at most ${PRODUCT_DESCRIPTION_TEXT_MAX} characters.`,
        )
      return { ...base, value_text: value }
    case 'NUMBER': {
      if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(value))
        throw new Error(`${attribute.name}: enter a decimal number.`)
      const decimal = toDecimal(value)
      if (
        !decimal.isFinite() ||
        decimal.decimalPlaces() > 4 ||
        decimal.abs().gte('100000000000000')
      ) {
        throw new Error(`${attribute.name}: use at most 14 whole digits and 4 decimal places.`)
      }
      return { ...base, value_number: value }
    }
    case 'BOOLEAN':
      if (value !== 'true' && value !== 'false')
        throw new Error(`${attribute.name}: choose Yes or No.`)
      return { ...base, value_bool: value === 'true' }
    case 'OPTION':
      if (!attribute.options.includes(value))
        throw new Error(`${attribute.name}: choose a permitted option.`)
      return { ...base, value_option: value }
  }
}

/** Validate the complete replacement before a save can drop or persist any value. */
export function specificationReplacement(
  attributes: readonly SpecificationAttribute[],
  drafts: Readonly<Record<string, string>>,
): { values: SpecificationValueInput[]; errors: Record<string, string> } {
  const values: SpecificationValueInput[] = []
  const errors: Record<string, string> = {}
  for (const attribute of attributes) {
    try {
      const value = specificationInput(attribute, drafts[attribute.id] ?? '')
      if (value) values.push(value)
    } catch (error) {
      errors[attribute.id] =
        error instanceof Error ? error.message : 'Enter a valid specification value.'
    }
  }
  if (values.length > SPECIFICATION_VALUE_MAX)
    errors._root = `Record at most ${SPECIFICATION_VALUE_MAX} specification values.`
  return { values, errors }
}

/** Master/template changes also alter item completeness and dealer descriptions. */
export function invalidateProductDescriptions(client: QueryClient) {
  void client.invalidateQueries({ queryKey: queryKeys.specifications.all })
  void client.invalidateQueries({ queryKey: queryKeys.items.all })
  void client.invalidateQueries({ queryKey: queryKeys.dealerPortal.all })
}

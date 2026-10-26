import type { SchemaOf } from '@/types/api'

export function specificationAttribute(
  overrides: Partial<SchemaOf<'SpecificationAttributeRead'>> = {},
): SchemaOf<'SpecificationAttributeRead'> {
  return {
    id: 'attribute-1',
    code: 'MATERIAL',
    name: 'Material',
    value_type: 'OPTION',
    unit: null,
    options: ['Brass', 'Zinc Alloy'],
    is_filterable: true,
    sort_order: 0,
    is_active: true,
    created_at: '2026-10-26T00:00:00Z',
    updated_at: '2026-10-26T00:00:00Z',
    ...overrides,
  }
}

export function itemSpecifications(
  overrides: Partial<SchemaOf<'ItemSpecificationsRead'>> = {},
): SchemaOf<'ItemSpecificationsRead'> {
  return { item_id: 'item-1', values: [], template: [], missing_required_count: 0, ...overrides }
}

export function itemSpecificationValue(
  attribute: SchemaOf<'SpecificationAttributeRead'>,
  overrides: Partial<SchemaOf<'ItemSpecificationValueRead'>> = {},
): SchemaOf<'ItemSpecificationValueRead'> {
  return {
    id: `value-${attribute.id}`,
    specification_attribute_id: attribute.id,
    value_type: attribute.value_type,
    value_text: null,
    value_number: null,
    value_bool: null,
    value_option: null,
    attribute,
    ...overrides,
  }
}

export function itemAccessory(
  overrides: Partial<SchemaOf<'ItemAccessoryRead'>> = {},
): SchemaOf<'ItemAccessoryRead'> {
  return {
    id: 'accessory-1',
    item_id: 'item-1',
    accessory_item_id: 'screw-kit',
    accessory_code: 'SCREW-KIT',
    accessory_name: 'Fixing screw kit',
    accessory_is_active: true,
    accessory_can_sell: false,
    quantity: '1.25',
    relation: 'INCLUDED',
    sort_order: 10,
    notes: 'Packed with the handle',
    ...overrides,
  }
}

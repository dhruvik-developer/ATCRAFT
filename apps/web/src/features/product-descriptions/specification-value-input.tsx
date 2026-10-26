import { Input } from '@/components/ui/primitives'
import { PRODUCT_DESCRIPTION_TEXT_MAX, SELECT_NONE } from '@/lib/constants'

import type { SpecificationAttribute } from './specification-model'

export function SpecificationValueInput({
  attribute,
  id,
  value,
  onChange,
  disabled,
  invalid,
}: {
  attribute: SpecificationAttribute
  id: string
  value: string
  onChange: (value: string) => void
  disabled?: boolean
  invalid?: boolean
}) {
  if (attribute.value_type === 'BOOLEAN' || attribute.value_type === 'OPTION') {
    return (
      <select
        id={id}
        value={value || SELECT_NONE}
        onChange={(event) => onChange(event.target.value === SELECT_NONE ? '' : event.target.value)}
        disabled={disabled}
        aria-invalid={invalid}
        className="h-11 w-full min-w-0 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
      >
        <option value={SELECT_NONE}>Not recorded</option>
        {attribute.value_type === 'BOOLEAN' ? (
          <>
            <option value="true">Yes</option>
            <option value="false">No</option>
          </>
        ) : (
          attribute.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))
        )}
      </select>
    )
  }
  return (
    <Input
      id={id}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      inputMode={attribute.value_type === 'NUMBER' ? 'decimal' : 'text'}
      maxLength={attribute.value_type === 'TEXT' ? PRODUCT_DESCRIPTION_TEXT_MAX : undefined}
      disabled={disabled}
      aria-invalid={invalid}
      className="min-h-11 min-w-0"
    />
  )
}

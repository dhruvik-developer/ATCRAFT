/**
 * The password policy, mirrored from `src.core.security.validate_password_policy`.
 *
 * Duplicated in the browser for one reason only: a live checklist is far kinder
 * than submitting a form and being told the password was wrong in four separate
 * ways. The server remains the authority - it also rejects common passwords and
 * anything containing the account's own email address, neither of which this
 * copy can check exhaustively, and a 422 from it is rendered on the field.
 */

import { z } from 'zod'

export const PASSWORD_MIN_LENGTH = 12
export const PASSWORD_MAX_LENGTH = 128

export interface PasswordRule {
  id: string
  label: string
  satisfied: (password: string) => boolean
}

export const PASSWORD_RULES: PasswordRule[] = [
  {
    id: 'length',
    label: `At least ${PASSWORD_MIN_LENGTH} characters`,
    satisfied: (value) => value.length >= PASSWORD_MIN_LENGTH,
  },
  { id: 'lower', label: 'A lowercase letter', satisfied: (value) => /[a-z]/.test(value) },
  { id: 'upper', label: 'An uppercase letter', satisfied: (value) => /[A-Z]/.test(value) },
  { id: 'digit', label: 'A digit', satisfied: (value) => /\d/.test(value) },
  {
    id: 'symbol',
    label: 'A symbol, for example ! @ # or -',
    satisfied: (value) => /[^A-Za-z0-9]/.test(value),
  },
]

export function unmetRules(password: string): PasswordRule[] {
  return PASSWORD_RULES.filter((rule) => !rule.satisfied(password))
}

/** Zod field honouring the same rules, with one message per failed rule. */
export const passwordField = z
  .string()
  .max(PASSWORD_MAX_LENGTH, `At most ${PASSWORD_MAX_LENGTH} characters`)
  .superRefine((value, ctx) => {
    for (const rule of unmetRules(value)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: `Missing: ${rule.label.toLowerCase()}` })
    }
  })

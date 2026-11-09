import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

/*
 * `no-restricted-syntax` entries, named so that a file exempt from one keeps
 * the rest: a later config block replaces a rule's whole option list rather
 * than merging into it.
 */
const destructiveButtonClassName = {
  // twMerge keeps `text-destructive` beside the variant's
  // `hover:text-accent-foreground`, and the hover rule wins.
  selector:
    "JSXOpeningElement[name.name='Button'] > JSXAttribute[name.name='className'][value.value=/(^|\\s)text-destructive(\\s|$)/]",
  message:
    "Use variant='destructive-outline' rather than colouring a Button with className: the variant's hover colour overrides text-destructive under the pointer.",
}

const utcDateSlice = {
  // `toISOString()` is UTC, so a slice of it is the UTC date or wall clock - in
  // India still yesterday until 05:30. A credit note took its date this way and
  // posted to the previous day's ledger, on 1 April into the previous year.
  selector:
    "CallExpression[callee.property.name=/^(slice|substring|substr)$/][callee.object.type='CallExpression'][callee.object.callee.property.name='toISOString']",
  message:
    'A slice of toISOString() is the UTC date or time, not the local one. Use toDateInput / toDateTimeInput from lib/format.ts (today is toDateInput(new Date())), or its date-only helpers for calendar arithmetic.',
}

export default tseslint.config(
  { ignores: ['dist', 'node_modules', 'coverage'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // Fast Refresh wants one component per module. Two patterns here are
      // idiomatic and worth keeping: cva variant objects living beside the
      // component they style, and a context module exporting its provider plus
      // the hooks that read it. Naming them is what allowExportNames is for -
      // the rule still guards every other file.
      'react-refresh/only-export-components': [
        'warn',
        {
          allowConstantExport: true,
          allowExportNames: [
            'buttonVariants',
            'badgeVariants',
            'alertVariants',
            'useAuth',
            'useTheme',
          ],
        },
      ],
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      '@typescript-eslint/no-explicit-any': 'warn',
      'no-console': ['warn', { allow: ['warn', 'error'] }],
      // A native prompt ignores the theme, blocks the tab, and answers null
      // wherever a kiosk or embedded browser suppresses dialogs - so the action
      // it guards silently does nothing. Confirm with ConfirmDialog, and collect
      // an audit reason with ReasonDialog (components/shared.tsx).
      'no-alert': 'error',
      'no-restricted-syntax': ['error', destructiveButtonClassName, utcDateSlice],
    },
  },
  {
    // The one home of date conversion, including the calendar arithmetic that
    // is UTC on purpose because it involves no instant.
    files: ['src/lib/format.ts'],
    rules: {
      'no-restricted-syntax': ['error', destructiveButtonClassName],
    },
  },
)

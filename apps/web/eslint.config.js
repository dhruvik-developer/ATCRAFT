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

const decimalThroughNumber = {
  // API quantities and money are decimal strings computed with decimal.js
  // (lib/format.ts). Number() on one is a float: a credit note printed its
  // remaining quantity, 10.3 less 10.1, as 0.20000000000000107.
  selector:
    'CallExpression[callee.name=/^(Number|parseFloat)$/] > MemberExpression[property.name=/(quantity|amount|price|total|rate)$/]',
  message:
    'An API decimal is a string for decimal.js: use toDecimal(...) from lib/format.ts to compute or compare it, and formatQuantity / formatMoney to show it.',
}

const queryClientConstruction = {
  // A second client is a second cache: an invalidation in one never reaches the
  // other. The screen-test harness built its own and dropped the staleTimes and
  // gcTime, so no test could see a mutation that forgot to invalidate a cache.
  selector: "NewExpression[callee.name='QueryClient']",
  message:
    'Build a query client with createQueryClient() from lib/query-client.ts, which carries the staleTimes, retry policy and error nets; in a screen test use renderScreen, and its cachePolicy option.',
}

// A key whose first element is a literal names a root the registry does not
// know, so nothing ties it to the prefix its writers invalidate: five caches -
// dealers, the raise dialog's templates, the inbound carriers among them - went
// on serving stale rows after the writes that changed them. The registry in
// lib/query-client.ts holds every root; a feature factory spreads one.
const literalKeyHead = '[elements.0.type=/^(Literal|TemplateLiteral)$/]'
const keyProperty = 'Property[key.name=/^(queryKey|mutationKey)$/]'
const literalQueryKey = {
  selector: [
    `${keyProperty} > ArrayExpression${literalKeyHead}`,
    `${keyProperty} > TSAsExpression > ArrayExpression${literalKeyHead}`,
    `${keyProperty} > ArrowFunctionExpression > ArrayExpression${literalKeyHead}`,
    `${keyProperty} > ArrowFunctionExpression > TSAsExpression > ArrayExpression${literalKeyHead}`,
    `JSXAttribute[name.name='queryKey'] ArrayExpression${literalKeyHead}`,
    `CallExpression[callee.property.name=/^(getQueryData|setQueryData|getQueryState|setQueryDefaults)$/] > ArrayExpression:first-child${literalKeyHead}`,
    // A key factory (`dealerKeys = { ... }`) or a single key (`pendingKey`).
    `VariableDeclarator[id.name=/Keys$/] > ObjectExpression ArrayExpression${literalKeyHead}`,
    `VariableDeclarator[id.name=/Keys$/] > TSAsExpression > ObjectExpression ArrayExpression${literalKeyHead}`,
    `VariableDeclarator[id.name=/(Key|KEY)$/] > ArrayExpression${literalKeyHead}`,
    `VariableDeclarator[id.name=/(Key|KEY)$/] > TSAsExpression > ArrayExpression${literalKeyHead}`,
    `VariableDeclarator[id.name=/(Key|KEY)$/] > ArrowFunctionExpression > ArrayExpression${literalKeyHead}`,
    `VariableDeclarator[id.name=/(Key|KEY)$/] > ArrowFunctionExpression > TSAsExpression > ArrayExpression${literalKeyHead}`,
  ].join(', '),
  message:
    'Root a query key in queryKeys (lib/query-client.ts): use its key, or spread one of its roots into a feature factory. A literal root is a cache no known invalidation reaches.',
}

const nativeSelect = {
  // Thirty-three hand-styled selects drifted ten ways: 40 and 44px beside the
  // 36px Input, the panel border instead of the input one, 16px text, no red
  // for aria-invalid and no disabled look - an editable-looking control that
  // was not, and a refused field that showed nothing.
  selector: "JSXOpeningElement[name.name='select']",
  message:
    "Use NativeSelect from '@/components/ui/primitives' (size 'sm' | 'touch' where the screen needs it), which draws a native select the way Input is drawn.",
}

const nativeCheckbox = {
  // Ten filter and form checkboxes drew the browser's own control - its accent
  // colour and its size - beside the Checkbox every other screen uses.
  selector:
    "JSXOpeningElement[name.name='input'] > JSXAttribute[name.name='type'][value.value='checkbox']",
  message:
    "Use Checkbox from '@/components/ui/primitives' inside the Label, with onCheckedChange={(checked) => ... checked === true ...}.",
}

const queryFnSignal = {
  // TanStack aborts a query whose function read the signal once no screen
  // observes it - the search a keystroke superseded, the dialog that closed.
  // 213 query functions never read it, so every one of those ran to completion.
  selector:
    "Property[key.name='queryFn'] > :matches(ArrowFunctionExpression, FunctionExpression):not(:has(Identifier[name='signal']))",
  message:
    'Destructure { signal } from the query function context and pass it to the api call (api.get(path, { params, signal })), or to the helper that makes it, so a superseded or abandoned read is cancelled.',
}

/*
 * `no-restricted-properties` entries, named for the same reason as the syntax
 * ones above.
 *
 * A locale method formats in the browser's locale, not DEFAULT_LOCALE: every
 * paginated table's total read 100,000 beside the 1,00,000 its own cells
 * showed. lib/format.ts holds the one formatter cache, pinned to en-IN.
 */
const localeFormatting = ['toLocaleString', 'toLocaleDateString', 'toLocaleTimeString'].map(
  (property) => ({
    property,
    message: `${property} formats in the browser's locale, not the application's. Use formatNumber / formatMoney / formatDate / formatDateTime from lib/format.ts.`,
  }),
)

/*
 * Browser storage throws wherever site data is blocked, and AppLayout reading it
 * bare during render took every signed-in screen down on such a machine.
 * lib/storage.ts is the guarded way in; tests set up storage directly.
 */
const storageMessage = (name) =>
  `${name} throws wherever site data is blocked. Use readStorage / writeStorage / removeStorage from lib/storage.ts.`
const browserStorageGlobals = ['localStorage', 'sessionStorage'].map((name) => ({
  name,
  message: storageMessage(name),
}))
const browserStorageProperties = ['window', 'globalThis'].flatMap((object) =>
  ['localStorage', 'sessionStorage'].map((property) => ({
    object,
    property,
    message: storageMessage(property),
  })),
)

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
      'no-restricted-syntax': [
        'error',
        destructiveButtonClassName,
        utcDateSlice,
        decimalThroughNumber,
        queryClientConstruction,
        literalQueryKey,
        queryFnSignal,
        nativeSelect,
        nativeCheckbox,
      ],
      'no-restricted-properties': ['error', ...localeFormatting, ...browserStorageProperties],
      'no-restricted-globals': ['error', ...browserStorageGlobals],
    },
  },
  {
    // The design-system primitives draw the native elements every screen uses.
    files: ['src/components/ui/**'],
    rules: {
      'no-restricted-syntax': [
        'error',
        destructiveButtonClassName,
        utcDateSlice,
        decimalThroughNumber,
        queryClientConstruction,
        literalQueryKey,
        queryFnSignal,
      ],
    },
  },
  {
    // The one home of value conversion: the calendar arithmetic that is UTC on
    // purpose because it involves no instant, and the final Number() a
    // formatter takes once all decimal arithmetic is done.
    files: ['src/lib/format.ts'],
    rules: {
      'no-restricted-syntax': [
        'error',
        destructiveButtonClassName,
        queryClientConstruction,
        literalQueryKey,
        queryFnSignal,
      ],
      'no-restricted-properties': ['error', ...browserStorageProperties],
    },
  },
  {
    // The one constructor of a query client, and the registry of key roots.
    files: ['src/lib/query-client.ts'],
    rules: {
      'no-restricted-syntax': [
        'error',
        destructiveButtonClassName,
        utcDateSlice,
        decimalThroughNumber,
        queryFnSignal,
      ],
    },
  },
  {
    // A test's probe component keys a cache no screen reads, a test may spell
    // a key out to pin its shape, and a probe's query function may answer a
    // constant without any request to cancel.
    files: ['src/test/**', '**/*.test.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': [
        'error',
        destructiveButtonClassName,
        utcDateSlice,
        decimalThroughNumber,
        queryClientConstruction,
      ],
    },
  },
  {
    // The guarded way into browser storage, and the tests that arrange it.
    files: ['src/lib/storage.ts', 'src/test/**', '**/*.test.{ts,tsx}'],
    rules: {
      'no-restricted-properties': ['error', ...localeFormatting],
      'no-restricted-globals': 'off',
    },
  },
)

/**
 * Map a 422 from the API onto the form control that caused it.
 *
 * The mutation cache suppresses the toast for a 422 that carries field errors
 * (lib/query-client), on the understanding that the form shows them inline.
 * This is the other half of that arrangement, so the two have to stay in step -
 * which is why there is exactly one copy of it, here, next to the api client
 * that shapes `fieldErrors` and the query client that decides to stay quiet.
 *
 * Two keys have no control behind them. `_root` is a model-level refusal whose
 * path is the whole body, and `query.*` names a query parameter. Both land on
 * the form's `root` error, which a dialog renders above its footer as
 * `errors.root?.message`, and the mutation cache toasts them as well - so a
 * form that has no root slot still says something. `partitionFieldErrors` is
 * the one definition of "orphan" both sides read.
 */

import type { FieldPath, FieldValues, UseFormSetError } from 'react-hook-form'

import { ApiError } from '@/lib/api-client'
import { joinValidationMessages } from '@/lib/messages'
import { toast } from '@/lib/toast'

/** A 422 key outside the form body: transport parameters, or the whole body. */
export function isOrphanField(field: string): boolean {
  return (
    field === '_root' ||
    ['query', 'path', 'header', 'cookie'].some(
      (source) => field === source || field.startsWith(`${source}.`),
    )
  )
}

export interface PartitionedFieldErrors {
  /** Keys a form control can own, still keyed by the API's dotted path. */
  fields: Record<string, string[]>
  /** Messages under `_root` or `query.*`, in the order the API sent them. */
  orphans: string[]
}

/** Split a 422's field errors into those a control can show and those nothing can. */
export function partitionFieldErrors(error: ApiError): PartitionedFieldErrors {
  const fields: Record<string, string[]> = {}
  const orphans: string[] = []
  for (const [field, messages] of Object.entries(error.fieldErrors)) {
    const nonempty = messages.filter((message) => message.trim())
    if (nonempty.length === 0) continue
    if (isOrphanField(field)) orphans.push(...nonempty)
    else fields[field] = nonempty
  }
  return { fields, orphans }
}

/**
 * Put each field error on its control, and the orphans on `root`.
 *
 * The first refused control that can take the focus takes it, which also
 * scrolls it into view. A server error otherwise changed nothing a person could
 * see: a long form is submitted from its footer, the mutation cache keeps the
 * toast back for an error the form shows inline, and the message landed under
 * a field scrolled out of sight at the top of the dialog body.
 *
 * Which control can take it is found by trying, in the API's order, until the
 * focus moves. react-hook-form focuses a field only through the element its
 * registration holds, and a `Controller` whose render never hands `field.ref`
 * to an element holds none, so `shouldFocus` does nothing for it - nor for a
 * disabled or hidden control. When no refused control takes the focus, the
 * first error the refusal renders is brought into view, and the control it
 * sits under is focused when it has one (`revealFirstFieldError`).
 *
 * `mapPath` rewrites the API's path before it is looked up on the form, for a
 * form whose payload is not shaped exactly like its fields - see
 * `remapArrayIndices` for the common case.
 */
export function applyFieldErrors<T extends FieldValues>(
  error: unknown,
  setError: UseFormSetError<T>,
  mapPath?: (path: string) => string,
): void {
  if (!(error instanceof ApiError)) return
  const { fields, orphans } = partitionFieldErrors(error)
  const before = focusedElement()
  let focused = false
  const messages: string[] = []
  for (const [field, fieldMessages] of Object.entries(fields)) {
    // The API sends dotted paths such as `lines.0.quantity`, which is exactly
    // how react-hook-form addresses a field array entry.
    const path = mapPath ? mapPath(field) : field
    const message = joinValidationMessages(fieldMessages)
    messages.push(message)
    setError(path as FieldPath<T>, { type: 'server', message }, { shouldFocus: !focused })
    focused ||= tookFocus(before, path)
  }
  if (orphans.length > 0) {
    setError('root', { type: 'server', message: joinValidationMessages(orphans) })
  }
  if (!focused && messages.length > 0) revealFirstFieldError(messages)
}

function focusedElement(): Element | null {
  return typeof document === 'undefined' ? null : document.activeElement
}

/**
 * Whether a refused control holds the focus now: it moved, or it was already
 * on the control registered under `path` - a field submitted with Enter.
 */
function tookFocus(before: Element | null, path: string): boolean {
  const now = focusedElement()
  return now !== before || (now !== null && now.getAttribute('name') === path)
}

/**
 * How many frames to look for a refusal's errors before giving up. React
 * renders them in the task after `setError`, which is almost always before the
 * first frame; the rest allow for a busy main thread.
 */
const REVEAL_FRAMES = 10

/** What a person can move the focus to. */
const FOCUSABLE = 'input, select, textarea, button, [tabindex]'

/**
 * Bring the first error a refusal rendered into view, and focus the control it
 * sits under when it has one - for a refusal none of whose controls could take
 * the focus itself.
 *
 * The error is the first alert in the document that carries one of the
 * refusal's own messages, which is what `FieldError` renders, outside anything
 * a modal dialog has hidden from the person.
 */
function revealFirstFieldError(messages: readonly string[]): void {
  if (typeof requestAnimationFrame !== 'function') return
  const wanted = messages.map((message) => message.trim())
  let frames = REVEAL_FRAMES
  const look = () => {
    const shown = [...document.querySelectorAll<HTMLElement>('[role="alert"]')].find(
      (alert) =>
        wanted.includes(alert.textContent?.trim() ?? '') &&
        !alert.closest('[aria-hidden="true"], [inert]'),
    )
    if (shown) {
      reveal(shown)
      return
    }
    frames -= 1
    if (frames > 0) requestAnimationFrame(look)
  }
  requestAnimationFrame(look)
}

function reveal(error: HTMLElement): void {
  const control = controlOf(error)
  control?.focus()
  // Focusing scrolls the control into view; the line under it may still be
  // just out of sight. Without a control, the line is centred so the field
  // above it shows as well.
  error.scrollIntoView({ block: control ? 'nearest' : 'center' })
}

/**
 * The control a field error is about, inside the error's own parent - the
 * label, the control and the error are how every form here lays out a field.
 * The last label before the error names it when there is one; otherwise it is
 * the nearest control before the error. Null when that is nothing a person can
 * focus.
 */
function controlOf(error: HTMLElement): HTMLElement | null {
  let labelled: HTMLElement | null | undefined
  let nearest: HTMLElement | null = null
  const candidates = error.parentElement?.querySelectorAll<HTMLElement>(`label[for], ${FOCUSABLE}`)
  for (const element of candidates ?? []) {
    if (!(element.compareDocumentPosition(error) & Node.DOCUMENT_POSITION_FOLLOWING)) break
    if (element instanceof HTMLLabelElement) labelled = document.getElementById(element.htmlFor)
    else if (canFocus(element)) nearest = element
  }
  if (labelled === undefined) return nearest
  return labelled && canFocus(labelled) ? labelled : null
}

function canFocus(element: HTMLElement): boolean {
  return (
    element.tabIndex >= 0 &&
    !(element as HTMLButtonElement).disabled &&
    element.getAttribute('type') !== 'hidden' &&
    !element.closest('[aria-hidden="true"], [inert]')
  )
}

/**
 * A `mapPath` for a field array the form posts sparsely.
 *
 * When blank rows are dropped before posting, the API's `lines.2.quantity`
 * names the third row *sent*, not the third row on screen. `sent` lists, for
 * each index in the payload, the index of the form row it came from, so the
 * error lands on the row the user can see.
 */
export function remapArrayIndices(name: string, sent: readonly number[]): (path: string) => string {
  const prefix = `${name}.`
  return (path) => {
    if (!path.startsWith(prefix)) return path

    const rest = path.slice(prefix.length)
    const separator = rest.indexOf('.')
    const rawIndex = separator === -1 ? rest : rest.slice(0, separator)
    if (!/^\d+$/.test(rawIndex)) return path

    const suffix = separator === -1 ? '' : rest.slice(separator)
    return `${prefix}${sent[Number(rawIndex)] ?? rawIndex}${suffix}`
  }
}

/**
 * Every message from a 422, joined - for a control that is not a
 * react-hook-form field (the lone reason box in a confirm dialog, say) to show
 * under itself. Null for anything that is not an `ApiError` with field detail,
 * so the caller can leave its slot alone.
 */
export function fieldErrorSummary(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null
  const messages = Object.values(error.fieldErrors).flat()
  return joinValidationMessages(messages) || null
}

/** "lines" -> "line", "entries" -> "entry", "batches" -> "batch"; anything else as it is. */
function singular(noun: string): string {
  if (noun.endsWith('ies')) return `${noun.slice(0, -3)}y`
  if (/(?:ss|x|ch|sh)es$/.test(noun)) return noun.slice(0, -2)
  if (/[^s]s$/.test(noun)) return noun.slice(0, -1)
  return noun
}

/**
 * A 422 key in words, for a message that has to name its field.
 *
 * The API's keys are dotted body paths, zero-based and snake_case
 * (`lines.0.quantity`), while every screen numbers its lines from one - so the
 * raw key sent an operator to "line 0", the wrong line, under a column name.
 * An index takes the place of its collection's name: `lines.0.quantity` reads
 * "Line 1 quantity", `promised_date` "Promised date".
 */
export function fieldLabel(path: string): string {
  const words: string[] = []
  for (const segment of path.split('.')) {
    if (/^\d+$/.test(segment)) {
      const collection = words.pop()
      words.push(`${collection ? singular(collection) : 'line'} ${Number(segment) + 1}`)
    } else if (segment) {
      words.push(segment.replace(/_/g, ' '))
    }
  }
  const label = words.join(' ')
  return label.charAt(0).toUpperCase() + label.slice(1)
}

/**
 * Surface a 422 that the mutation cache deliberately stays quiet about.
 *
 * The cache suppresses the toast for a 422 carrying field errors on the
 * understanding a form renders them inline. A mutation fired from a confirm
 * dialog has no form, so without this a rejected `reason` or line set produced
 * no message anywhere. Orphans are left out here because the cache already
 * toasts those itself; every other status still toasts globally.
 */
export function reportFieldErrors(error: unknown, title: string): void {
  if (!(error instanceof ApiError) || error.status !== 422) return
  const fields = Object.entries(partitionFieldErrors(error).fields)
  if (fields.length === 0) return
  toast.error(
    title,
    fields
      .map(([field, messages]) => `${fieldLabel(field)}: ${joinValidationMessages(messages)}`)
      .join(' - '),
  )
}

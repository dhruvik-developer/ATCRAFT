import {
  createElement,
  lazy,
  Suspense,
  useState,
  type ComponentProps,
  type LazyExoticComponent,
  type ReactElement,
  type ReactNode,
} from 'react'

import { ErrorBoundary } from '@/components/error-boundary'
import { EAGER_PRELOAD } from '@/lib/constants'
import { browserSavesData, whenIdle } from '@/lib/preload'
import { LazyDialogFallback, type LazyDialogProps } from './lazy-dialog-fallback'

type DialogModule = { default: (props: never) => ReactNode }

/**
 * A form kept out of its screen's first download, then fetched eagerly as soon
 * as the screen's own code has run - in the background after sign-in, with
 * every other screen the person can open - so the screen appears sooner and the
 * form still opens without waiting. It keeps its existing mount/reset
 * behaviour. Once its code is in memory every opening renders the form at once;
 * only an opening before the download has finished shows a loading state.
 */
export function lazyDialog<Module extends DialogModule>(
  loader: () => Promise<Module>,
): (props: Parameters<Module['default']>[0] & LazyDialogProps) => ReactElement | null
export function lazyDialog(loader: () => Promise<DialogModule>) {
  type Props = LazyDialogProps
  type Form = (props: Props) => ReactNode
  let pending: Promise<{ default: Form }> | undefined
  /** The form itself, once its code has arrived. */
  let loaded: Form | undefined
  const load = () => {
    pending ??= loader()
      .then((module) => {
        // Props come from this exact export. The function view bridges React's
        // conditional ref types without widening any caller's form props.
        loaded = module.default as unknown as Form
        return { default: loaded }
      })
      .catch((error: unknown) => {
        pending = undefined
        throw error
      })
    return pending
  }
  // A fresh lazy() suspends for one render even when the module is already in
  // memory, which would put the loading dialog - and focus - in front of the
  // form on every opening. So a lazy() only stands in while the code is away.
  const content = (): Form | LazyExoticComponent<Form> => loaded ?? lazy(load)

  // Eager loading starts when the screen's module runs, not when the form
  // mounts: a screen that mounts a form only once it is opened would otherwise
  // fetch it on the click (lib/preload.ts). A failed download is left for the
  // form to retry when it is opened.
  if (EAGER_PRELOAD && !browserSavesData()) {
    whenIdle(() => {
      load().catch(() => undefined)
    })
  }

  return function DeferredDialog(props: Props) {
    // What the form renders as, chosen the first time it opens and kept from
    // then on: it stays mounted between openings, and a re-render never swaps
    // the element type under a half-filled form.
    const [Content, setContent] = useState(() => (props.open ? content() : null))
    const [attempt, setAttempt] = useState(0)

    if (Content === null) {
      if (props.open) setContent(content)
      return null
    }

    const retry = () => {
      setContent(content)
      setAttempt((value) => value + 1)
    }

    return createElement(ErrorBoundary, {
      key: attempt,
      fallback: createElement(LazyDialogFallback, { ...props, onRetry: retry }),
      children: createElement(
        Suspense,
        { fallback: createElement(LazyDialogFallback, props) },
        createElement(Content, props as ComponentProps<typeof Content>),
      ),
    })
  }
}

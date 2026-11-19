import {
  createElement,
  lazy,
  Suspense,
  useEffect,
  useState,
  type ComponentProps,
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
 * as the screen has painted, so the screen appears sooner and the form still
 * opens without waiting. It keeps its existing mount/reset behaviour; if the
 * download has not finished when the form is opened, a loading state shows.
 */
export function lazyDialog<Module extends DialogModule>(
  loader: () => Promise<Module>,
): (props: Parameters<Module['default']>[0] & LazyDialogProps) => ReactElement | null
export function lazyDialog(loader: () => Promise<DialogModule>) {
  type Props = LazyDialogProps
  type Loaded = { default: (props: Props) => ReactNode }
  let pending: Promise<Loaded> | undefined
  const load = () => {
    pending ??= loader()
      .then((module): Loaded => ({
        // Props come from this exact export. The function view bridges React's
        // conditional ref types without widening any caller's form props.
        default: module.default as unknown as Loaded['default'],
      }))
      .catch((error: unknown) => {
        pending = undefined
        throw error
      })
    return pending
  }

  return function DeferredDialog(props: Props) {
    const [opened, setOpened] = useState(props.open)
    const [attempt, setAttempt] = useState(0)
    const [Content, setContent] = useState(() => lazy(load))

    useEffect(() => {
      if (props.open) setOpened(true)
    }, [props.open])

    // Eager loading: fetch the form's code as soon as its screen has painted,
    // so opening it never waits on the network (lib/preload.ts).
    useEffect(() => {
      if (!EAGER_PRELOAD || browserSavesData()) return undefined
      return whenIdle(() => {
        load().catch(() => undefined)
      })
    }, [])

    if (!props.open && !opened) return null

    const retry = () => {
      setContent(() => lazy(load))
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

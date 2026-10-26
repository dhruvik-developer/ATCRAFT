/** Types shared by the browser sweeps and their TypeScript regressions. */
import type { BrowserContext, Page } from '@playwright/test'

export function createNavigator(page: Page): {
  navigate(route: string): Promise<void>
  settle(): Promise<void>
}

export function errorToastFailures(page: Page): Promise<string[]>
export function isReadinessDialogOpener(label: string, type: string | null): boolean
export const SESSION_ACTIONS: string[]
export function allowsReadOnlyRequest(
  method: string,
  requestUrl: string,
  apiUrl: string,
  browserUrl?: string,
): boolean
export function protectReadOnlyContext(
  context: BrowserContext,
  apiUrl: string,
  browserUrl?: string,
): Promise<string[]>

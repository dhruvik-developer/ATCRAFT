/** Types shared by the browser sweeps and their TypeScript regressions. */
import type { Page } from '@playwright/test'

export function createNavigator(page: Page): {
  navigate(route: string): Promise<void>
  settle(): Promise<void>
}

export function errorToastFailures(page: Page): Promise<string[]>

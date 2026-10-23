/** Types for `session.mjs`, so the TypeScript specs read it with a real contract. */

import type { Page } from '@playwright/test'

export interface ApiReply {
  status: number
  body: any
}

export interface Account {
  email?: string
  password?: string
}

export interface BootstrapAccount extends Account {
  bootstrapPassword?: string
}

export interface BrowserAccount extends Account {
  allowTemporaryPassword?: boolean
}

export function postJson(path: string, payload: unknown, token?: string): Promise<ApiReply>
export function login(email: string, password: string): Promise<ApiReply>
export function readyAdminToken(account?: BootstrapAccount): Promise<string>
export function signInThroughForm(page: Page, account?: BrowserAccount): Promise<void>

import type { BootstrapAccount } from './session.mjs'

export type Audience = 'staff' | 'vendor' | 'dealer'
export function portalIdentityPath(audience: Exclude<Audience, 'staff'>): string
export function routeAudience(route: string): Audience
export function accountForAudience(
  audience: Audience,
  environment?: Record<string, string | undefined>,
): BootstrapAccount & { email: string; password: string }

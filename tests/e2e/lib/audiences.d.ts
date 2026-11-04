import type { BootstrapAccount } from './session.mjs'

export type Audience = 'staff' | 'vendor' | 'vendor-job-work' | 'dealer'
export type PortalAudience = Exclude<Audience, 'staff'>
export function portalIdentityPath(audience: PortalAudience): string
export function routeAudience(route: string): Audience
export function accountForAudience(
  audience: Audience,
  environment?: Record<string, string | undefined>,
): BootstrapAccount & { email: string; password: string }

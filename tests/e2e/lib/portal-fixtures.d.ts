import type { PortalAudience } from './audiences.mjs'

export function ensurePortalAccounts(
  token: string,
  environment?: Record<string, string | undefined>,
  audiences?: PortalAudience[],
): Promise<void>
export function assertPortalData(
  audience: PortalAudience,
  token: string,
): Promise<Record<string, string>>

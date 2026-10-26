export function ensurePortalAccounts(
  token: string,
  environment?: Record<string, string | undefined>,
  audiences?: ('vendor' | 'dealer')[],
): Promise<void>
export function assertPortalData(
  audience: 'vendor' | 'dealer',
  token: string,
): Promise<Record<string, string>>

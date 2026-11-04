import { ADMIN_EMAIL, ADMIN_PASSWORD, BOOTSTRAP_PASSWORD } from './env.mjs'

/** Both portal home screens resolve this scoped identity after session restore. */
export function portalIdentityPath(audience) {
  if (!['vendor', 'vendor-job-work', 'dealer'].includes(audience))
    throw new Error(`Unknown portal audience: ${audience}`)
  return `/${audience === 'vendor-job-work' ? 'vendor' : audience}-portal/me`
}

/** Shared screens use staff; portal routes must use their own bound principal. */
export function routeAudience(route) {
  const pathname = route.split('?')[0]
  if (pathname === '/vendor/job-work' || pathname.startsWith('/vendor/job-work/'))
    return 'vendor-job-work'
  if (pathname === '/vendor' || pathname.startsWith('/vendor/')) return 'vendor'
  if (pathname === '/dealer' || pathname.startsWith('/dealer/')) return 'dealer'
  return 'staff'
}

export function accountForAudience(audience, environment = process.env) {
  if (audience === 'staff')
    return { email: ADMIN_EMAIL, password: ADMIN_PASSWORD, bootstrapPassword: BOOTSTRAP_PASSWORD }
  const prefixes = {
    vendor: 'E2E_VENDOR',
    'vendor-job-work': 'E2E_JOB_WORK_VENDOR',
    dealer: 'E2E_DEALER',
  }
  const prefix = prefixes[audience]
  if (!prefix)
    throw new Error(`Unknown screen audience: ${audience}`)
  const email = environment[`${prefix}_EMAIL`]
  const password = environment[`${prefix}_PASSWORD`]
  if (!email || !password)
    throw new Error(
      `Set ${prefix}_EMAIL and ${prefix}_PASSWORD to a configured ${audience} account. Portal coverage cannot use the staff administrator.`,
    )
  return { email, password, bootstrapPassword: environment[`${prefix}_BOOTSTRAP_PASSWORD`] ?? '' }
}

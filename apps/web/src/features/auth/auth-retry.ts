import { ApiError } from '@/lib/api-client'
import { AUTH_RETRY_AFTER_SECONDS } from '@/lib/constants'

/** A finite wait keeps authentication retries from immediately hitting the limiter again. */
export function authenticationRetryAfter(error: unknown): number | null {
  if (!(error instanceof ApiError) || error.status !== 429) return null
  const raw = error.details?.retry_after_seconds
  const seconds = typeof raw === 'number' ? raw : Number(raw)
  return Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : AUTH_RETRY_AFTER_SECONDS
}

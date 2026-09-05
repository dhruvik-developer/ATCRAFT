/**
 * The staff directory as a picker list.
 *
 * Four screens needed the same thing - a department head, a machine operator,
 * a document uploader, an audit actor - and each grew its own copy. They were
 * byte-for-byte the same request under three different cache keys and two
 * different stale windows, so a user who visited Organisation, Production and
 * Documents downloaded the directory three times a session and the copies could
 * disagree with each other for up to half an hour. The Documents copy was keyed
 * under `documents` rather than `users`, so the invalidation after a user was
 * created or deactivated never reached it at all.
 *
 * A directory is bounded in a way the transactional tables are not, so one page
 * of `LOOKUP_LIST_LIMIT` is the whole list for any realistic company. A caller
 * whose account may not read users gets an empty list and a select that offers
 * only "unassigned", rather than a broken screen - `GET /users` enforces
 * `user.view`, and a missing name is decoration on these screens, not the point
 * of them. Only that refusal is swallowed; an outage still surfaces.
 */

import { useQuery } from '@tanstack/react-query'

import { api, emptyOnPermissionError } from '@/lib/api-client'
import { LOOKUP_LIST_LIMIT } from '@/lib/constants'
import { queryKeys, staleTimes } from '@/lib/query-client'

/** The fields every caller reads. Rows carry more; nothing here depends on it. */
export interface UserOption {
  id: string
  full_name: string
  email: string
}

export interface UserOptionsInput {
  /**
   * Offer only staff who can be assigned something today. The audit actor
   * filter passes `false`: a log entry written by someone who has since left
   * still needs their name against it.
   */
  activeOnly?: boolean
  enabled?: boolean
}

export function useUserOptions<T = UserOption>({
  activeOnly = true,
  enabled = true,
}: UserOptionsInput = {}) {
  return useQuery({
    queryKey: queryKeys.users.options(activeOnly),
    queryFn: async (): Promise<T[]> => {
      try {
        const page = await api.getPage<T>('/users', {
          params: {
            limit: LOOKUP_LIST_LIMIT,
            ...(activeOnly ? { is_active: true } : {}),
            sort_by: 'full_name',
            sort_dir: 'asc',
            with_total: false,
          },
        })
        return page.data
      } catch (error) {
        return emptyOnPermissionError<T>(error)
      }
    },
    staleTime: staleTimes.reference,
    enabled,
  })
}

'use client'

import { useAuth } from '@clerk/nextjs'
import { useEffect, useMemo, useState } from 'react'
import { createApiClient, readErrorBody } from '../../lib/api'

export type AdminAccessState = 'checking' | 'granted' | 'denied'

/**
 * Shared auth/access plumbing for the admin panel. Every tab gets its own
 * data fetching, but they all go through the same authorizedFetch and the
 * same one-time admin gate check, so there's a single source of truth for
 * "is this visitor actually allowed to be here" -- the real enforcement is
 * still server-side (every /admin/* endpoint independently checks
 * require_admin), this is just what decides whether to render the panel
 * or an access-denied state.
 */
export function useAdmin() {
  const { getToken, isLoaded, isSignedIn } = useAuth()
  const { authorizedFetch } = useMemo(() => createApiClient(getToken), [getToken])
  const [access, setAccess] = useState<AdminAccessState>('checking')

  useEffect(() => {
    if (!isLoaded) return
    if (!isSignedIn) {
      setAccess('denied')
      return
    }
    let cancelled = false
    authorizedFetch('/admin/me')
      .then(response => {
        if (!cancelled) setAccess(response.ok ? 'granted' : 'denied')
      })
      .catch(() => {
        if (!cancelled) setAccess('denied')
      })
    return () => {
      cancelled = true
    }
  }, [isLoaded, isSignedIn])

  return { authorizedFetch, readErrorBody, access }
}

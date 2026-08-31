'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { createApiClient, readErrorBody } from '../../lib/api'
import { useFroneiAuth } from '../../lib/auth'

export type CelpipAccess = 'checking' | 'granted' | 'denied'

/**
 * Auth plumbing for the CELPIP workspace. Identical in shape to the admin
 * panel's useAdmin: this decides whether to render the workspace, while the
 * real enforcement stays server-side -- every /admin/celpip/* endpoint checks
 * require_admin independently.
 */
export function useCelpip() {
  const { getToken, isLoaded, isSignedIn } = useFroneiAuth()
  const { authorizedFetch } = useMemo(() => createApiClient(getToken), [getToken])
  const [access, setAccess] = useState<CelpipAccess>('checking')

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

  const getJson = useCallback(
    async <T,>(path: string, init?: RequestInit): Promise<T> => {
      const response = await authorizedFetch(path, init)
      if (!response.ok) {
        throw new Error(await readErrorBody(response, `Request failed (${response.status})`))
      }
      return (await response.json()) as T
    },
    [authorizedFetch],
  )

  const postJson = useCallback(
    async <T,>(path: string, body?: unknown): Promise<T> =>
      getJson<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
    [getJson],
  )

  // Memoised: this object is passed as a prop and read in effect dependency
  // arrays. Returning a fresh literal every render meant that any re-render of
  // the shell -- including the one Clerk triggers when it refreshes the session
  // token, roughly once a minute -- changed its identity and re-ran those
  // effects. In the session runner that reloaded the current question and
  // unmounted it, cutting listening audio off mid-playback.
  return useMemo(
    () => ({ authorizedFetch, getJson, postJson, readErrorBody, access }),
    [authorizedFetch, getJson, postJson, access],
  )
}

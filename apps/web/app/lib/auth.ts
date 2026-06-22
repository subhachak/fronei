'use client'

import { useAuth } from '@clerk/nextjs'
import { e2eAuthBypassEnabled } from './e2e'

export function useFroneiAuth() {
  if (e2eAuthBypassEnabled()) {
    return {
      getToken: async () => 'e2e-auth-bypass-token',
      isLoaded: true,
      isSignedIn: true,
    }
  }

  return useAuth()
}

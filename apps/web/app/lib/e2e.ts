export function e2eAuthBypassEnabled() {
  return process.env.NODE_ENV !== 'production' && process.env.NEXT_PUBLIC_E2E_AUTH_BYPASS === 'true'
}

export function e2eProxyBypassEnabled() {
  return process.env.NODE_ENV !== 'production' && process.env.E2E_AUTH_BYPASS === 'true'
}

export function assertNoProductionE2EBypass() {
  if (
    process.env.NODE_ENV === 'production'
    && (process.env.E2E_AUTH_BYPASS === 'true' || process.env.NEXT_PUBLIC_E2E_AUTH_BYPASS === 'true')
  ) {
    throw new Error('E2E auth bypass flags must never be enabled in production.')
  }
}

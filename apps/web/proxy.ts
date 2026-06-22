import { clerkMiddleware, createRouteMatcher } from '@clerk/nextjs/server'
import { NextResponse } from 'next/server'
import { assertNoProductionE2EBypass, e2eProxyBypassEnabled } from './app/lib/e2e'

const isPublic = createRouteMatcher(['/sign-in(.*)', '/sign-up(.*)'])

assertNoProductionE2EBypass()

const proxy = e2eProxyBypassEnabled()
  ? () => NextResponse.next()
  : clerkMiddleware(async (auth, req) => {
    if (!isPublic(req)) await auth.protect()
  })

export default proxy

export const config = {
  matcher: [
    '/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico)).*)',
    '/(api|trpc)(.*)',
  ],
}

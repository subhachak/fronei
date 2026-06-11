import { clerkMiddleware, createRouteMatcher } from '@clerk/nextjs/server'

const isPublic = createRouteMatcher(['/sign-in(.*)', '/sign-up(.*)'])

const proxy = clerkMiddleware(async (auth, req) => {
  if (!isPublic(req)) await auth.protect()
})

export default proxy

export const config = {
  matcher: [
    '/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico)).*)',
    '/(api|trpc)(.*)',
  ],
}

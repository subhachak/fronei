'use client'

import { notFound } from 'next/navigation'
import { useEffect, useMemo, useState } from 'react'
import { createApiClient } from '../lib/api'
import type { BlogPost } from '../lib/blog'
import { useFroneiAuth } from '../lib/auth'
import { PostView } from './PostView'

/**
 * Renders when the public GET /blog/posts/{slug} 404s -- either the slug
 * doesn't exist, or it's a draft. Only an authenticated admin gets a second
 * chance here, via the admin-only by-slug lookup (which sees drafts). Any
 * other visitor -- signed out, or signed in but not admin -- gets a normal
 * 404, since a 403 from the admin endpoint is indistinguishable from "no
 * such post" for this purpose.
 */
export function DraftPreviewGate({ slug }: { slug: string }) {
  const { getToken, isLoaded, isSignedIn } = useFroneiAuth()
  const { authorizedFetch } = useMemo(() => createApiClient(getToken), [getToken])
  const [state, setState] = useState<'checking' | 'found' | 'not-found'>('checking')
  const [post, setPost] = useState<BlogPost | null>(null)

  useEffect(() => {
    if (!isLoaded) return
    if (!isSignedIn) {
      setState('not-found')
      return
    }
    let cancelled = false
    authorizedFetch(`/admin/blog/posts/by-slug/${encodeURIComponent(slug)}`)
      .then(async response => {
        if (cancelled) return
        if (!response.ok) {
          setState('not-found')
          return
        }
        setPost(await response.json() as BlogPost)
        setState('found')
      })
      .catch(() => {
        if (!cancelled) setState('not-found')
      })
    return () => {
      cancelled = true
    }
  }, [isLoaded, isSignedIn, slug])

  if (state === 'checking') {
    return <main className="mx-auto w-full max-w-3xl px-6 py-16 text-center text-stone-400">Loading…</main>
  }
  if (state === 'not-found' || !post) {
    notFound()
  }
  return <PostView post={post} isDraftPreview={post.status !== 'published'} />
}

import type { Metadata } from 'next'
import Link from 'next/link'
import { listPublishedPosts } from '../../../lib/blog'
import { PostCard } from '../../PostCard'

export const revalidate = 60

export async function generateMetadata({ params }: { params: Promise<{ tag: string }> }): Promise<Metadata> {
  const { tag } = await params
  return { title: `#${decodeURIComponent(tag)} — Fronei Blog` }
}

export default async function BlogTagPage({ params }: { params: Promise<{ tag: string }> }) {
  const { tag } = await params
  const decodedTag = decodeURIComponent(tag)
  const posts = await listPublishedPosts(decodedTag)

  return (
    <main className="mx-auto w-full max-w-3xl px-6 py-16">
      <Link href="/blog" className="text-sm font-medium text-stone-500 hover:text-stone-900">← All posts</Link>
      <h1 className="mt-3 font-[family-name:var(--font-marketing-serif)] text-4xl font-semibold text-stone-900">
        #{decodedTag}
      </h1>
      <div className="mt-10 divide-y divide-stone-200">
        {posts.length === 0 ? (
          <p className="py-16 text-center text-stone-400">No posts tagged #{decodedTag} yet.</p>
        ) : (
          posts.map(post => <PostCard key={post.id} post={post} />)
        )}
      </div>
    </main>
  )
}

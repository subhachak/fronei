import type { Metadata } from 'next'
import Link from 'next/link'
import { allTags, listPublishedPosts } from '../lib/blog'
import { PostCard } from './PostCard'

export const metadata: Metadata = {
  title: 'Blog — Fronei',
  description: 'Notes on Fronei and other projects, technology and AI opinions, and predictions.',
}

export const revalidate = 60

export default async function BlogIndexPage() {
  const posts = await listPublishedPosts()
  const tags = allTags(posts)

  return (
    <main className="mx-auto w-full max-w-3xl px-6 py-16">
      <h1 className="font-[family-name:var(--font-marketing-serif)] text-4xl font-semibold text-stone-900">
        Blog
      </h1>
      <p className="mt-3 text-stone-600">
        Notes on what I&apos;m building, technology and AI opinions, and predictions.
      </p>

      {tags.length > 0 && (
        <div className="mt-6 flex flex-wrap gap-2">
          {tags.map(tag => (
            <Link
              key={tag}
              href={`/blog/tag/${encodeURIComponent(tag)}`}
              className="rounded-full border border-stone-300 px-3 py-1 text-sm text-stone-600 hover:border-stone-500 hover:text-stone-900"
            >
              #{tag}
            </Link>
          ))}
        </div>
      )}

      <div className="mt-10 divide-y divide-stone-200">
        {posts.length === 0 ? (
          <p className="py-16 text-center text-stone-400">Nothing published yet — check back soon.</p>
        ) : (
          posts.map(post => <PostCard key={post.id} post={post} />)
        )}
      </div>
    </main>
  )
}

import Link from 'next/link'
import { MarkdownResult } from '../components/MarkdownResult'
import type { BlogPost } from '../lib/blog'

function formatDate(value: string | null): string {
  if (!value) return 'Unpublished'
  return new Date(value).toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' })
}

export function PostView({ post, isDraftPreview = false }: { post: BlogPost; isDraftPreview?: boolean }) {
  return (
    <main className="mx-auto w-full max-w-3xl px-6 py-16">
      {isDraftPreview && (
        <div className="mb-8 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm font-medium text-amber-800">
          Draft preview — this post isn&apos;t published yet. Only admins can see this page.
        </div>
      )}
      <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-stone-400">
        <span>{formatDate(post.published_at)}</span>
        <span>·</span>
        <span>{post.author}</span>
        {post.voice === 'product' && (
          <span className="rounded-full bg-stone-200 px-2 py-0.5 normal-case tracking-normal text-stone-600">
            Product update
          </span>
        )}
      </div>
      <h1 className="mt-2 font-[family-name:var(--font-marketing-serif)] text-4xl font-semibold text-stone-900">
        {post.title}
      </h1>
      {post.excerpt && <p className="mt-3 text-lg text-stone-600">{post.excerpt}</p>}
      <div className="mt-8 border-t border-stone-200 pt-8">
        <MarkdownResult content={post.body_markdown} />
      </div>
      {post.tags.length > 0 && (
        <div className="mt-10 flex flex-wrap gap-2 border-t border-stone-200 pt-6 text-xs text-stone-400">
          {post.tags.map(tag => (
            <Link key={tag} href={`/blog/tag/${encodeURIComponent(tag)}`} className="hover:text-stone-700">
              #{tag}
            </Link>
          ))}
        </div>
      )}
    </main>
  )
}

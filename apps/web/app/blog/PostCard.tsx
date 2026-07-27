import Link from 'next/link'
import type { BlogPost } from '../lib/blog'

function formatDate(value: string | null): string {
  if (!value) return ''
  return new Date(value).toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' })
}

export function PostCard({ post }: { post: BlogPost }) {
  return (
    <article className="py-8 first:pt-0">
      <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-stone-400">
        <span>{formatDate(post.published_at)}</span>
        {post.voice === 'product' && (
          <span className="rounded-full bg-stone-200 px-2 py-0.5 text-stone-600">Product update</span>
        )}
      </div>
      <h2 className="mt-2 font-[family-name:var(--font-marketing-serif)] text-2xl font-semibold text-stone-900">
        <Link href={`/blog/${post.slug}`} className="hover:underline">{post.title}</Link>
      </h2>
      {post.excerpt && <p className="mt-2 text-stone-600">{post.excerpt}</p>}
      {post.tags.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2 text-xs text-stone-400">
          {post.tags.map(tag => (
            <Link key={tag} href={`/blog/tag/${encodeURIComponent(tag)}`} className="hover:text-stone-700">
              #{tag}
            </Link>
          ))}
        </div>
      )}
    </article>
  )
}

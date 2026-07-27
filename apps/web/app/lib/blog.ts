const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000'

export type BlogPostVoice = 'personal' | 'product'

export type BlogPost = {
  id: string
  slug: string
  title: string
  excerpt: string
  body_markdown: string
  tags: string[]
  author: string
  voice: BlogPostVoice
  status: 'draft' | 'published'
  cover_image_url: string | null
  published_at: string | null
  created_at: string
  updated_at: string
}

export async function listPublishedPosts(tag?: string): Promise<BlogPost[]> {
  const query = tag ? `?tag=${encodeURIComponent(tag)}` : ''
  try {
    const response = await fetch(`${API_BASE}/blog/posts${query}`, { next: { revalidate: 60 } })
    if (!response.ok) return []
    const body = await response.json() as { items: BlogPost[] }
    return body.items
  } catch {
    return []
  }
}

export async function getPublishedPost(slug: string): Promise<BlogPost | null> {
  try {
    const response = await fetch(`${API_BASE}/blog/posts/${encodeURIComponent(slug)}`, { next: { revalidate: 60 } })
    if (!response.ok) return null
    return await response.json() as BlogPost
  } catch {
    return null
  }
}

export function allTags(posts: BlogPost[]): string[] {
  const seen = new Set<string>()
  for (const post of posts) for (const tag of post.tags) seen.add(tag)
  return Array.from(seen).sort()
}

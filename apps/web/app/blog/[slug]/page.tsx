import type { Metadata } from 'next'
import { getPublishedPost } from '../../lib/blog'
import { DraftPreviewGate } from '../DraftPreviewGate'
import { PostView } from '../PostView'

export const revalidate = 60

export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }): Promise<Metadata> {
  const { slug } = await params
  const post = await getPublishedPost(slug)
  if (!post) return { title: 'Fronei Blog' }
  return {
    title: `${post.title} — Fronei Blog`,
    description: post.excerpt || undefined,
    openGraph: {
      title: post.title,
      description: post.excerpt || undefined,
      type: 'article',
    },
  }
}

export default async function BlogPostPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params
  const post = await getPublishedPost(slug)
  if (post) return <PostView post={post} />
  return <DraftPreviewGate slug={slug} />
}

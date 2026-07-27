'use client'

import { Eye, Loader2, Pencil, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { MarkdownResult } from '../../components/MarkdownResult'
import { readErrorBody } from '../../lib/api'
import { formatAppDateTime } from '../../lib/format'
import type { AuthorizedFetch, BlogPost, BlogPostVoice } from '../types'

const INPUT_CLASS =
  'w-full rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 px-3 py-2 text-sm text-neutral-900 dark:text-neutral-50 focus:outline-none focus:ring-2 focus:ring-neutral-400'
const LABEL_CLASS = 'mb-1 block text-xs font-semibold text-neutral-500'

const STATUS_STYLES: Record<BlogPost['status'], string> = {
  draft: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  published: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
}

type EditorForm = {
  title: string
  slug: string
  excerpt: string
  body_markdown: string
  tags: string
  author: string
  voice: BlogPostVoice
  cover_image_url: string
}

function emptyForm(): EditorForm {
  return {
    title: '',
    slug: '',
    excerpt: '',
    body_markdown: '',
    tags: '',
    author: 'Subh Chakraborty',
    voice: 'personal',
    cover_image_url: '',
  }
}

function formFromPost(post: BlogPost): EditorForm {
  return {
    title: post.title,
    slug: post.slug,
    excerpt: post.excerpt,
    body_markdown: post.body_markdown,
    tags: post.tags.join(', '),
    author: post.author,
    voice: post.voice,
    cover_image_url: post.cover_image_url || '',
  }
}

export function BlogTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  const [posts, setPosts] = useState<BlogPost[] | null>(null)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [editing, setEditing] = useState<BlogPost | 'new' | null>(null)
  const [form, setForm] = useState<EditorForm>(emptyForm())
  const [saving, setSaving] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)

  const load = useCallback(async () => {
    setRefreshing(true)
    try {
      const response = await authorizedFetch('/admin/blog/posts')
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load posts'))
      const body = await response.json() as { items: BlogPost[] }
      setPosts(body.items)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load posts')
    } finally {
      setRefreshing(false)
    }
  }, [authorizedFetch])

  useEffect(() => { void load() }, [load])

  function set<K extends keyof EditorForm>(key: K, value: EditorForm[K]) {
    setForm(prev => ({ ...prev, [key]: value }))
  }

  function startNew() {
    setForm(emptyForm())
    setEditing('new')
  }

  function startEdit(post: BlogPost) {
    setForm(formFromPost(post))
    setEditing(post)
  }

  async function save() {
    setSaving(true)
    setError('')
    try {
      const payload = {
        title: form.title,
        slug: form.slug || undefined,
        excerpt: form.excerpt,
        body_markdown: form.body_markdown,
        tags: form.tags.split(',').map(t => t.trim()).filter(Boolean),
        author: form.author,
        voice: form.voice,
        cover_image_url: form.cover_image_url || null,
      }
      const isNew = editing === 'new'
      const response = await authorizedFetch(
        isNew ? '/admin/blog/posts' : `/admin/blog/posts/${(editing as BlogPost).id}`,
        { method: isNew ? 'POST' : 'PATCH', body: JSON.stringify(payload) },
      )
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not save post'))
      const saved = await response.json() as BlogPost
      setEditing(saved)
      setForm(formFromPost(saved))
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save post')
    } finally {
      setSaving(false)
    }
  }

  async function togglePublish(post: BlogPost) {
    setBusyId(post.id)
    try {
      const action = post.status === 'published' ? 'unpublish' : 'publish'
      const response = await authorizedFetch(`/admin/blog/posts/${post.id}/${action}`, { method: 'POST' })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not update post'))
      await load()
      if (editing !== 'new' && editing?.id === post.id) {
        setEditing(await response.json() as BlogPost)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not update post')
    } finally {
      setBusyId(null)
    }
  }

  async function remove(post: BlogPost) {
    if (!window.confirm(`Delete "${post.title}"? This can't be undone.`)) return
    setBusyId(post.id)
    try {
      const response = await authorizedFetch(`/admin/blog/posts/${post.id}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not delete post'))
      if (editing !== 'new' && editing?.id === post.id) setEditing(null)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not delete post')
    } finally {
      setBusyId(null)
    }
  }

  if (editing) {
    const currentPost = editing === 'new' ? null : editing
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <button
            type="button"
            onClick={() => setEditing(null)}
            className="text-xs font-semibold text-neutral-500 hover:text-neutral-900 dark:hover:text-neutral-100"
          >
            ← Back to posts
          </button>
          <div className="flex items-center gap-2">
            {currentPost && (
              <a
                href={`/blog/${currentPost.slug}`}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 rounded-lg border border-neutral-200 px-3 py-2 text-xs font-semibold text-neutral-600 hover:bg-neutral-50 dark:border-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-800"
              >
                <Eye size={13} /> Preview
              </a>
            )}
            {currentPost && (
              <button
                type="button"
                onClick={() => void togglePublish(currentPost)}
                disabled={busyId === currentPost.id}
                className="rounded-lg bg-neutral-900 px-3 py-2 text-xs font-semibold text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
              >
                {currentPost.status === 'published' ? 'Unpublish' : 'Publish'}
              </button>
            )}
            <button
              type="button"
              onClick={() => void save()}
              disabled={saving || !form.title.trim()}
              className="inline-flex items-center gap-1.5 rounded-lg bg-neutral-900 px-4 py-2 text-xs font-semibold text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
            >
              {saving && <Loader2 size={13} className="animate-spin" />}
              Save
            </button>
          </div>
        </div>

        {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div className="space-y-3">
            <div>
              <label className={LABEL_CLASS}>Title *</label>
              <input value={form.title} onChange={e => set('title', e.target.value)} className={INPUT_CLASS} />
            </div>
            <div>
              <label className={LABEL_CLASS}>
                Slug <span className="font-normal text-neutral-400">(leave blank to auto-generate from title)</span>
              </label>
              <input value={form.slug} onChange={e => set('slug', e.target.value)} placeholder="auto-generated" className={INPUT_CLASS} />
            </div>
            <div>
              <label className={LABEL_CLASS}>Excerpt</label>
              <textarea rows={2} value={form.excerpt} onChange={e => set('excerpt', e.target.value)} className={`${INPUT_CLASS} resize-none`} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className={LABEL_CLASS}>Author</label>
                <input value={form.author} onChange={e => set('author', e.target.value)} className={INPUT_CLASS} />
              </div>
              <div>
                <label className={LABEL_CLASS}>Voice</label>
                <select value={form.voice} onChange={e => set('voice', e.target.value as BlogPostVoice)} className={INPUT_CLASS}>
                  <option value="personal">Personal</option>
                  <option value="product">Product update</option>
                </select>
              </div>
            </div>
            <div>
              <label className={LABEL_CLASS}>Tags <span className="font-normal text-neutral-400">(comma-separated)</span></label>
              <input value={form.tags} onChange={e => set('tags', e.target.value)} placeholder="ai, predictions, projects" className={INPUT_CLASS} />
            </div>
            <div>
              <label className={LABEL_CLASS}>Cover image URL</label>
              <input value={form.cover_image_url} onChange={e => set('cover_image_url', e.target.value)} className={INPUT_CLASS} />
            </div>
            <div>
              <label className={LABEL_CLASS}>Body (Markdown)</label>
              <textarea
                rows={20}
                value={form.body_markdown}
                onChange={e => set('body_markdown', e.target.value)}
                className={`${INPUT_CLASS} resize-none font-mono`}
              />
            </div>
          </div>

          <div className="lg:sticky lg:top-0 lg:self-start">
            <label className={LABEL_CLASS}>Live preview</label>
            <div className="max-h-[80vh] overflow-y-auto rounded-lg border border-neutral-200 bg-white p-5 dark:border-neutral-700 dark:bg-neutral-900">
              <h1 className="text-2xl font-bold text-neutral-900 dark:text-neutral-50">{form.title || 'Untitled post'}</h1>
              {form.excerpt && <p className="mt-2 text-sm text-neutral-500">{form.excerpt}</p>}
              <MarkdownResult content={form.body_markdown} />
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Blog posts</h2>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void load()}
            disabled={refreshing}
            className="grid h-8 w-8 place-items-center rounded-md border border-neutral-200 text-neutral-500 disabled:opacity-50 dark:border-neutral-800 dark:text-neutral-400"
            aria-label="Refresh posts"
          >
            <RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} />
          </button>
          <button
            type="button"
            onClick={startNew}
            className="inline-flex items-center gap-1.5 rounded-lg bg-neutral-900 px-3 py-2 text-xs font-semibold text-white dark:bg-white dark:text-neutral-900"
          >
            <Plus size={14} /> New post
          </button>
        </div>
      </div>

      {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}

      {!posts ? (
        <div className="h-40 animate-pulse bg-neutral-100 dark:bg-neutral-900" />
      ) : posts.length === 0 ? (
        <p className="py-16 text-center text-sm text-neutral-400">No posts yet.</p>
      ) : (
        <div className="overflow-x-auto border border-neutral-200 dark:border-neutral-800">
          <table className="w-full min-w-[720px] border-collapse text-left text-xs">
            <thead className="bg-neutral-50 text-[10px] uppercase text-neutral-400 dark:bg-neutral-900">
              <tr>
                <th className="px-3 py-2.5">Status</th>
                <th className="px-3 py-2.5">Title</th>
                <th className="px-3 py-2.5">Tags</th>
                <th className="px-3 py-2.5">Updated</th>
                <th className="w-20 px-3 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {posts.map(post => (
                <tr key={post.id} className="border-t border-neutral-200 align-top dark:border-neutral-800">
                  <td className="px-3 py-3">
                    <span className={`inline-flex rounded px-2 py-1 font-bold ${STATUS_STYLES[post.status]}`}>
                      {post.status}
                    </span>
                    {post.voice === 'product' && (
                      <p className="mt-1 text-[10px] font-semibold text-neutral-400">Product update</p>
                    )}
                  </td>
                  <td className="px-3 py-3">
                    <button type="button" onClick={() => startEdit(post)} className="font-medium text-neutral-800 hover:underline dark:text-neutral-200">
                      {post.title}
                    </button>
                    <p className="mt-1 truncate text-[10px] text-neutral-400">{post.author}</p>
                  </td>
                  <td className="px-3 py-3 text-neutral-500">{post.tags.join(', ') || '—'}</td>
                  <td className="px-3 py-3 text-neutral-500">{formatAppDateTime(post.updated_at)}</td>
                  <td className="px-3 py-3">
                    <div className="flex items-center gap-1">
                      <button
                        type="button"
                        onClick={() => startEdit(post)}
                        className="grid h-7 w-7 place-items-center rounded-md text-neutral-400 hover:bg-neutral-100 hover:text-neutral-700 dark:hover:bg-neutral-800"
                        aria-label="Edit post"
                        title="Edit"
                      >
                        <Pencil size={13} />
                      </button>
                      <button
                        type="button"
                        onClick={() => void remove(post)}
                        disabled={busyId === post.id}
                        className="grid h-7 w-7 place-items-center rounded-md text-neutral-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-50 dark:hover:bg-red-950"
                        aria-label="Delete post"
                        title="Delete"
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

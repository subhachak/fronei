'use client'

import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { useMemo } from 'react'

export function MarkdownResult({ content }: { content: string }) {
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(content || '') as string), [content])
  return <div className="av3-markdown" dangerouslySetInnerHTML={{ __html: html }} />
}

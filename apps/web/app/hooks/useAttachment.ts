'use client'

/**
 * useAttachment — manages a single file attachment for the current message.
 *
 * Extracted from useAgent.ts (TD-06) to keep file-handling state isolated and
 * independently testable.
 */

import { useState } from 'react'
import { readErrorBody } from '../lib/api'
import type { AttachedFile } from '../types'

interface UseAttachmentOptions {
  authorizedFetch: (url: string, init?: RequestInit) => Promise<Response>
}

export function useAttachment({ authorizedFetch }: UseAttachmentOptions) {
  const [attachedFile, setAttachedFile] = useState<AttachedFile | null>(null)
  const [attachingFile, setAttachingFile] = useState(false)
  const [attachmentError, setAttachmentError] = useState('')
  const [supportedAttachmentTypes, setSupportedAttachmentTypes] = useState<string[]>([])

  async function loadSupportedAttachmentTypes() {
    try {
      const response = await authorizedFetch('/documents/supported')
      if (!response.ok) return
      const payload = await response.json() as { types: string[] }
      setSupportedAttachmentTypes(payload.types || [])
    } catch {
      // Non-critical: the file input falls back to accepting anything.
    }
  }

  async function attachFile(file: File | null) {
    if (!file) return
    setAttachmentError('')
    setAttachingFile(true)
    try {
      const form = new FormData()
      form.append('file', file)
      const response = await authorizedFetch('/documents/extract', { method: 'POST', body: form })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not read that file'))
      const payload = await response.json() as { name: string; text: string; char_count: number; truncated: boolean }
      setAttachedFile({
        name: payload.name || file.name,
        text: payload.text || '',
        charCount: payload.char_count || 0,
        truncated: Boolean(payload.truncated),
      })
    } catch (err) {
      setAttachmentError(err instanceof Error ? err.message : 'Could not read that file')
    } finally {
      setAttachingFile(false)
    }
  }

  function clearAttachment() {
    setAttachedFile(null)
    setAttachmentError('')
  }

  return {
    attachedFile,
    attachingFile,
    attachmentError,
    supportedAttachmentTypes,
    loadSupportedAttachmentTypes,
    attachFile,
    clearAttachment,
  }
}

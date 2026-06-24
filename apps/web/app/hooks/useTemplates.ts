'use client'

/**
 * useTemplates — manages user document templates (list, upload, delete).
 *
 * Extracted from useAgent.ts (TD-06) to keep template state isolated and
 * independently testable.
 */

import { useState } from 'react'
import { readErrorBody } from '../lib/api'
import type { DocumentTemplateOption } from '../types'

interface UseTemplatesOptions {
  authorizedFetch: (url: string, init?: RequestInit) => Promise<Response>
}

export function useTemplates({ authorizedFetch }: UseTemplatesOptions) {
  const [templates, setTemplates] = useState<DocumentTemplateOption[]>([])
  const [templatesLoaded, setTemplatesLoaded] = useState(false)
  const [templateStatus, setTemplateStatus] = useState('')
  const [templateError, setTemplateError] = useState('')
  const [templateDeleteId, setTemplateDeleteId] = useState<string | null>(null)
  const [selectedTemplateId, setSelectedTemplateId] = useState('')

  async function loadTemplates() {
    setTemplateError('')
    try {
      const response = await authorizedFetch('/documents/templates?doc_type=presentation')
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load templates'))
      const payload = await response.json() as { templates: DocumentTemplateOption[] }
      setTemplates(payload.templates || [])
      setTemplatesLoaded(true)
    } catch (err) {
      setTemplateError(err instanceof Error ? err.message : 'Could not load templates')
      setTemplatesLoaded(true)
    }
  }

  async function uploadTemplate(file: File | null, source: 'composer' | 'profile') {
    if (!file) return
    if (!file.name.toLowerCase().endsWith('.pptx')) {
      setTemplateError('Template must be a .pptx PowerPoint file.')
      return
    }
    setTemplateStatus(source === 'composer' ? 'Saving this template to your profile...' : 'Uploading template...')
    setTemplateError('')
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('name', file.name.replace(/\.pptx$/i, '').replace(/[-_]+/g, ' '))
      const response = await authorizedFetch('/documents/templates', { method: 'POST', body: form })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Template upload failed'))
      const uploaded = await response.json() as DocumentTemplateOption
      setTemplates(prev => [uploaded, ...prev.filter(t => t.id !== uploaded.id)])
      setSelectedTemplateId(uploaded.id)
      setTemplateStatus(source === 'composer' ? 'Template saved to your profile.' : 'Template uploaded.')
      setTemplatesLoaded(true)
    } catch (err) {
      setTemplateError(err instanceof Error ? err.message : 'Template upload failed')
      setTemplateStatus('')
    }
  }

  async function deleteTemplate(templateId: string) {
    setTemplateStatus('Deleting template...')
    setTemplateError('')
    try {
      const response = await authorizedFetch(`/documents/templates/${encodeURIComponent(templateId)}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Template delete failed'))
      setTemplates(prev => prev.filter(t => t.id !== templateId))
      if (selectedTemplateId === templateId) setSelectedTemplateId('')
      setTemplateDeleteId(null)
      setTemplateStatus('Template deleted.')
    } catch (err) {
      setTemplateError(err instanceof Error ? err.message : 'Template delete failed')
      setTemplateStatus('')
    }
  }

  const selectedTemplateExists = !selectedTemplateId || templates.some(t => t.id === selectedTemplateId)

  return {
    templates,
    templatesLoaded,
    templateStatus,
    templateError,
    templateDeleteId,
    setTemplateDeleteId,
    selectedTemplateId,
    setSelectedTemplateId,
    selectedTemplateExists,
    loadTemplates,
    uploadTemplate,
    deleteTemplate,
    refreshTemplates: loadTemplates,
  }
}

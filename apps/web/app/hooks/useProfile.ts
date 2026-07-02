'use client'

import { useCallback, useMemo, useState } from 'react'
import { createApiClient, readErrorBody } from '../lib/api'
import { useFroneiAuth } from '../lib/auth'
import type { DocumentTemplateOption, ProfileMe, ProfileSettings, ProfileUsage, ProfileWorkspace } from '../types'

export function useProfile() {
  const { getToken } = useFroneiAuth()
  const { authorizedFetch } = useMemo(() => createApiClient(getToken), [getToken])

  const [me, setMe] = useState<ProfileMe | null>(null)
  const [workspaces, setWorkspaces] = useState<ProfileWorkspace[] | null>(null)
  const [usage, setUsage] = useState<ProfileUsage | null>(null)
  const [templates, setTemplates] = useState<DocumentTemplateOption[]>([])
  const [templatesLoaded, setTemplatesLoaded] = useState(false)
  const [templateStatus, setTemplateStatus] = useState('')
  const [templateError, setTemplateError] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const loadMe = useCallback(async () => {
    try {
      const response = await authorizedFetch('/profile/me')
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load profile'))
      setMe(await response.json())
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load profile')
    }
  }, [authorizedFetch])

  const loadWorkspaces = useCallback(async () => {
    try {
      const response = await authorizedFetch('/profile/workspaces')
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load workspaces'))
      const payload = await response.json() as { workspaces: ProfileWorkspace[] }
      setWorkspaces(payload.workspaces)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load workspaces')
    }
  }, [authorizedFetch])

  const loadUsage = useCallback(async (range = '30d') => {
    try {
      const response = await authorizedFetch(`/profile/usage?range=${range}`)
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load usage'))
      setUsage(await response.json())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load usage')
    }
  }, [authorizedFetch])

  const loadTemplates = useCallback(async () => {
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
  }, [authorizedFetch])

  const loadAll = useCallback(async (range = '30d') => {
    setLoading(true)
    await Promise.all([loadMe(), loadWorkspaces(), loadUsage(range), loadTemplates()])
    setLoading(false)
  }, [loadMe, loadWorkspaces, loadUsage, loadTemplates])

  const updatePreferences = useCallback(async (preferences: string[]) => {
    const response = await authorizedFetch('/profile/preferences', {
      method: 'PATCH',
      body: JSON.stringify({ preferences }),
    })
    if (!response.ok) throw new Error(await readErrorBody(response, 'Could not update preferences'))
    const payload = await response.json() as { preferences: string[] }
    setMe(prev => prev ? { ...prev, preferences: payload.preferences } : prev)
    return payload.preferences
  }, [authorizedFetch])

  const removePreference = useCallback(async (item: string) => {
    const next = (me?.preferences || []).filter(p => p !== item)
    return updatePreferences(next)
  }, [me, updatePreferences])

  const updateSettings = useCallback(async (settings: Partial<ProfileSettings>) => {
    const response = await authorizedFetch('/profile/settings', {
      method: 'PATCH',
      body: JSON.stringify(settings),
    })
    if (!response.ok) throw new Error(await readErrorBody(response, 'Could not update settings'))
    const payload = await response.json() as ProfileSettings
    setMe(prev => prev ? { ...prev, settings: payload } : prev)
    return payload
  }, [authorizedFetch])

  const uploadTemplate = useCallback(async (file: File | null) => {
    if (!file) return null
    if (!file.name.toLowerCase().endsWith('.pptx')) {
      setTemplateError('Template must be a .pptx PowerPoint file.')
      return null
    }
    setTemplateStatus('Uploading template...')
    setTemplateError('')
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('name', file.name.replace(/\.pptx$/i, '').replace(/[-_]+/g, ' '))
      const response = await authorizedFetch('/documents/templates', { method: 'POST', body: form })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Template upload failed'))
      const uploaded = await response.json() as DocumentTemplateOption
      setTemplates(prev => [uploaded, ...prev.filter(template => template.id !== uploaded.id)])
      setTemplatesLoaded(true)
      setTemplateStatus('Template uploaded.')
      return uploaded
    } catch (err) {
      setTemplateError(err instanceof Error ? err.message : 'Template upload failed')
      setTemplateStatus('')
      return null
    }
  }, [authorizedFetch])

  const renameTemplate = useCallback(async (templateId: string, name: string) => {
    const trimmed = name.trim()
    if (!trimmed) return null
    setTemplateStatus('Renaming template...')
    setTemplateError('')
    try {
      const form = new FormData()
      form.append('name', trimmed)
      const response = await authorizedFetch(`/documents/templates/${encodeURIComponent(templateId)}`, { method: 'PATCH', body: form })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Template rename failed'))
      const renamed = await response.json() as DocumentTemplateOption
      setTemplates(prev => prev.map(template => template.id === templateId ? { ...template, ...renamed } : template))
      setTemplateStatus('Template renamed.')
      return renamed
    } catch (err) {
      setTemplateError(err instanceof Error ? err.message : 'Template rename failed')
      setTemplateStatus('')
      return null
    }
  }, [authorizedFetch])

  const replaceTemplate = useCallback(async (templateId: string, file: File | null) => {
    if (!file) return null
    if (!file.name.toLowerCase().endsWith('.pptx')) {
      setTemplateError('Replacement must be a .pptx PowerPoint file.')
      return null
    }
    setTemplateStatus('Replacing template...')
    setTemplateError('')
    try {
      const form = new FormData()
      form.append('file', file)
      const response = await authorizedFetch(`/documents/templates/${encodeURIComponent(templateId)}/replace`, { method: 'POST', body: form })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Template replace failed'))
      const replaced = await response.json() as DocumentTemplateOption
      setTemplates(prev => prev.map(template => template.id === templateId ? { ...template, ...replaced } : template))
      setTemplateStatus('Template replaced.')
      return replaced
    } catch (err) {
      setTemplateError(err instanceof Error ? err.message : 'Template replace failed')
      setTemplateStatus('')
      return null
    }
  }, [authorizedFetch])

  const deleteTemplate = useCallback(async (templateId: string) => {
    setTemplateStatus('Deleting template...')
    setTemplateError('')
    try {
      const response = await authorizedFetch(`/documents/templates/${encodeURIComponent(templateId)}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Template delete failed'))
      setTemplates(prev => prev.filter(template => template.id !== templateId))
      const wasDefault = me?.settings.default_template_id === templateId
      if (wasDefault) {
        const settingsResponse = await authorizedFetch('/profile/settings', {
          method: 'PATCH',
          body: JSON.stringify({ default_template_id: '' }),
        })
        if (settingsResponse.ok) {
          const payload = await settingsResponse.json() as ProfileSettings
          setMe(prev => prev ? { ...prev, settings: payload } : prev)
        }
      }
      setMe(prev => prev?.settings.default_template_id === templateId
        ? { ...prev, settings: { ...prev.settings, default_template_id: '' } }
        : prev)
      setTemplateStatus('Template deleted.')
      return true
    } catch (err) {
      setTemplateError(err instanceof Error ? err.message : 'Template delete failed')
      setTemplateStatus('')
      return false
    }
  }, [authorizedFetch, me])

  const updateWorkspacePriorities = useCallback(async (workspaceId: string, priorities: string[]) => {
    const response = await authorizedFetch(`/profile/workspaces/${encodeURIComponent(workspaceId)}/priorities`, {
      method: 'PATCH',
      body: JSON.stringify({ priorities }),
    })
    if (!response.ok) throw new Error(await readErrorBody(response, 'Could not update workspace priorities'))
    const payload = await response.json() as { workspace_id: string; priorities: string[] }
    setWorkspaces(prev => prev ? prev.map(w => (w.id === workspaceId ? { ...w, priorities: payload.priorities } : w)) : prev)
    return payload.priorities
  }, [authorizedFetch])

  const removeWorkspacePriority = useCallback(async (workspaceId: string, item: string) => {
    const workspace = workspaces?.find(w => w.id === workspaceId)
    const next = (workspace?.priorities || []).filter(p => p !== item)
    return updateWorkspacePriorities(workspaceId, next)
  }, [workspaces, updateWorkspacePriorities])

  const updateWorkspaceFacts = useCallback(async (workspaceId: string, facts: string[]) => {
    const response = await authorizedFetch(`/profile/workspaces/${encodeURIComponent(workspaceId)}/facts`, {
      method: 'PATCH',
      body: JSON.stringify({ facts }),
    })
    if (!response.ok) throw new Error(await readErrorBody(response, 'Could not update pinned facts'))
    const payload = await response.json() as { workspace_id: string; facts: string[] }
    setWorkspaces(prev => prev ? prev.map(w => (w.id === workspaceId ? { ...w, pinned_facts: payload.facts } : w)) : prev)
    return payload.facts
  }, [authorizedFetch])

  const removeWorkspaceFact = useCallback(async (workspaceId: string, item: string) => {
    const workspace = workspaces?.find(w => w.id === workspaceId)
    const next = (workspace?.pinned_facts || []).filter(fact => fact !== item)
    return updateWorkspaceFacts(workspaceId, next)
  }, [workspaces, updateWorkspaceFacts])

  const exportMyData = useCallback(async () => {
    const response = await authorizedFetch('/profile/export')
    if (!response.ok) throw new Error(await readErrorBody(response, 'Could not export data'))
    return response.json()
  }, [authorizedFetch])

  const deleteMyData = useCallback(async () => {
    const response = await authorizedFetch('/profile/privacy-delete', {
      method: 'POST',
      body: JSON.stringify({ confirm: true }),
    })
    if (!response.ok) throw new Error(await readErrorBody(response, 'Could not delete data'))
    return response.json()
  }, [authorizedFetch])

  return {
    me,
    workspaces,
    usage,
    templates,
    templatesLoaded,
    templateStatus,
    templateError,
    loading,
    error,
    setError,
    loadAll,
    loadUsage,
    loadTemplates,
    updatePreferences,
    removePreference,
    updateSettings,
    uploadTemplate,
    renameTemplate,
    replaceTemplate,
    deleteTemplate,
    updateWorkspacePriorities,
    removeWorkspacePriority,
    updateWorkspaceFacts,
    removeWorkspaceFact,
    exportMyData,
    deleteMyData,
  }
}

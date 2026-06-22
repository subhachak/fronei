'use client'

import { useCallback, useMemo, useState } from 'react'
import { createApiClient, readErrorBody } from '../lib/api'
import { useFroneiAuth } from '../lib/auth'
import type { ProfileMe, ProfileSettings, ProfileUsage, ProfileWorkspace } from '../types'

export function useProfile() {
  const { getToken } = useFroneiAuth()
  const { authorizedFetch } = useMemo(() => createApiClient(getToken), [getToken])

  const [me, setMe] = useState<ProfileMe | null>(null)
  const [workspaces, setWorkspaces] = useState<ProfileWorkspace[] | null>(null)
  const [usage, setUsage] = useState<ProfileUsage | null>(null)
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

  const loadAll = useCallback(async (range = '30d') => {
    setLoading(true)
    await Promise.all([loadMe(), loadWorkspaces(), loadUsage(range)])
    setLoading(false)
  }, [loadMe, loadWorkspaces, loadUsage])

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
    loading,
    error,
    setError,
    loadAll,
    loadUsage,
    updatePreferences,
    removePreference,
    updateSettings,
    updateWorkspacePriorities,
    removeWorkspacePriority,
    exportMyData,
    deleteMyData,
  }
}

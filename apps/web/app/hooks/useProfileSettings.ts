'use client'

/**
 * useProfileSettings — loads and persists user-level defaults (quality mode,
 * output format, research level, default template).
 *
 * Extracted from useAgent.ts (TD-06) to keep profile state isolated and
 * independently testable.
 */

import { useRef, useState } from 'react'
import { readErrorBody } from '../lib/api'
import type { OutputFormat, ProfileSettings, QualityMode, ResearchLevel } from '../types'

interface UseProfileSettingsOptions {
  authorizedFetch: (url: string, init?: RequestInit) => Promise<Response>
  onQualityModeChange: (mode: QualityMode) => void
  onOutputFormatChange: (format: OutputFormat) => void
  onResearchLevelChange: (level: ResearchLevel) => void
  onDefaultTemplateChange: (id: string) => void
  /** Ref that tracks whether the user has manually changed composer settings
   *  this session — if so, profile defaults should not overwrite them. */
  composerSettingsDirtyRef: React.RefObject<boolean>
}

export function useProfileSettings({
  authorizedFetch,
  onQualityModeChange,
  onOutputFormatChange,
  onResearchLevelChange,
  onDefaultTemplateChange,
  composerSettingsDirtyRef,
}: UseProfileSettingsOptions) {
  const [profileSettings, setProfileSettings] = useState<ProfileSettings>({})

  async function loadProfileSettings() {
    try {
      const response = await authorizedFetch('/profile/settings')
      if (!response.ok) return
      const settings = await response.json() as ProfileSettings
      setProfileSettings(settings)
      // Only apply remote defaults when the user hasn't overridden locally.
      if (composerSettingsDirtyRef.current) return
      if (settings.quality_mode) onQualityModeChange(settings.quality_mode)
      if (settings.output_format) onOutputFormatChange(settings.output_format)
      if (settings.research_level) onResearchLevelChange(settings.research_level)
      if (settings.default_template_id !== undefined) onDefaultTemplateChange(settings.default_template_id || '')
    } catch {
      // Non-critical: the composer still has local defaults.
    }
  }

  async function updateProfileSettings(settings: Partial<ProfileSettings>) {
    const response = await authorizedFetch('/profile/settings', {
      method: 'PATCH',
      body: JSON.stringify(settings),
    })
    if (!response.ok) throw new Error(await readErrorBody(response, 'Could not update profile settings'))
    const next = await response.json() as ProfileSettings
    setProfileSettings(next)
    if (next.quality_mode) onQualityModeChange(next.quality_mode)
    if (next.output_format) onOutputFormatChange(next.output_format)
    if (next.research_level) onResearchLevelChange(next.research_level)
    if (next.default_template_id !== undefined) onDefaultTemplateChange(next.default_template_id || '')
    return next
  }

  return {
    profileSettings,
    loadProfileSettings,
    updateProfileSettings,
  }
}

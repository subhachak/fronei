'use client'

import { useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'agent-v3-theme'

function readStoredTheme(): Theme | null {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return stored === 'light' || stored === 'dark' ? stored : null
  } catch {
    return null
  }
}

function applyTheme(theme: Theme) {
  document.documentElement.setAttribute('data-theme', theme)
  try {
    localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    /* ignore */
  }
}

/**
 * Theme control scoped to /agent-v3. Independent of the classic chat UI's
 * 'md-theme' localStorage key — each surface owns the shared `data-theme`
 * attribute on <html> while it is mounted, so the two never silently
 * overwrite each other's stored preference, but visiting either page
 * always reflects what was chosen on that page, on that device.
 */
export function useTheme() {
  const [theme, setThemeState] = useState<Theme>('dark')

  useEffect(() => {
    const stored = readStoredTheme()
    const initial = stored || (window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
    setThemeState(initial)
    applyTheme(initial)
  }, [])

  function setTheme(next: Theme) {
    setThemeState(next)
    applyTheme(next)
  }

  function toggleTheme() {
    setTheme(theme === 'dark' ? 'light' : 'dark')
  }

  return { theme, setTheme, toggleTheme }
}

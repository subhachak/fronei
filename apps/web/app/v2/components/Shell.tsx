'use client'

import { useEffect, useState } from 'react'
import { IconMenu2, IconX } from '@tabler/icons-react'
import { Button } from './ui/button'

interface ShellProps {
  sidebar: React.ReactNode | ((controls: SidebarControls) => React.ReactNode)
  conversation: React.ReactNode
  workPane?: React.ReactNode
  workPaneOpen?: boolean
}

type SidebarControls = {
  collapsed: boolean
  onToggleCollapse: () => void
}

export function Shell({ sidebar, conversation, workPane, workPaneOpen = false }: ShellProps) {
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const onToggleCollapse = () => setSidebarOpen(v => !v)
  const renderSidebar = (collapsed: boolean) => (
    typeof sidebar === 'function'
      ? sidebar({ collapsed, onToggleCollapse })
      : sidebar
  )

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'b') {
        e.preventDefault()
        setSidebarOpen(v => !v)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    const media = window.matchMedia('(max-width: 767px)')
    function sync() {
      if (media.matches) setSidebarOpen(false)
    }
    sync()
    media.addEventListener('change', sync)
    return () => media.removeEventListener('change', sync)
  }, [])

  return (
    <div className="relative flex h-full w-full overflow-hidden">
      <aside
        className={`hidden flex-none overflow-hidden border-r border-border bg-sidebar transition-all duration-200 md:block ${sidebarOpen ? 'w-60' : 'w-12'}`}
        aria-label="Navigation"
        data-collapsed={!sidebarOpen}
      >
        {renderSidebar(!sidebarOpen)}
      </aside>

      {drawerOpen && (
        <div className="fixed inset-0 z-40 md:hidden" role="presentation">
          <button
            className="absolute inset-0 h-full w-full cursor-default bg-black/45"
            aria-label="Close navigation"
            type="button"
            onClick={() => setDrawerOpen(false)}
          />
          <aside className="absolute inset-y-0 left-0 w-72 overflow-hidden border-r border-border bg-sidebar shadow-2xl" aria-label="Navigation drawer">
            <div className="absolute right-2 top-2 z-10">
              <Button variant="ghost" size="icon" type="button" aria-label="Close navigation" onClick={() => setDrawerOpen(false)}>
                <IconX className="h-4 w-4" />
              </Button>
            </div>
            {renderSidebar(false)}
          </aside>
        </div>
      )}

      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="flex h-12 items-center border-b border-border px-3 md:hidden">
          <Button variant="ghost" size="icon" type="button" aria-label="Open navigation" onClick={() => setDrawerOpen(true)}>
            <IconMenu2 className="h-5 w-5" />
          </Button>
        </div>
        {conversation}
      </main>

      <aside
        className={`flex-none overflow-hidden border-l border-border bg-card transition-all duration-200 ${workPaneOpen ? 'w-80' : 'w-0'}`}
        aria-label="Work pane"
      >
        {workPane}
      </aside>
    </div>
  )
}

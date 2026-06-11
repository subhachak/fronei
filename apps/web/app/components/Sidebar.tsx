'use client'

import { useEffect, useRef, useState, type MouseEvent } from 'react'
import Link from 'next/link'
import { SignOutButton, useUser } from '@clerk/nextjs'

export type ConversationSummary = {
  id: number; title: string; profile: string
  message_count: number; total_cost_usd?: number; created_at: string; updated_at: string
}

interface SidebarProps {
  activePage: 'chat' | 'dashboard' | 'admin'
  todaySpend?: number | null
  dailyBudget?: number
  conversations?: ConversationSummary[]
  activeConvId?: number | null
  onLoadConversation?: (id: number) => void
  onNewConversation?: () => void
  onDeleteConversation?: (e: MouseEvent, id: number) => void
  onExport?: (e: MouseEvent, conv: ConversationSummary) => void
  onDevModeChange?: (v: boolean) => void
  mobileNavOpen?: boolean
  deleteConfirmId?: number | null
  onRenameConversation?: (id: number, title: string) => void
  editingTitleId?: number | null
  editingTitle?: string
  onEditingTitleChange?: (v: string) => void
  onStartEdit?: (id: number, currentTitle: string) => void
  onCancelEdit?: () => void
  onOpenDashboard?: () => void
  onOpenSettings?: () => void
  settingsActive?: boolean
}

export default function Sidebar({
  activePage,
  todaySpend,
  dailyBudget = 10,
  conversations = [],
  activeConvId,
  onLoadConversation,
  onNewConversation,
  onDeleteConversation,
  onExport,
  onDevModeChange,
  mobileNavOpen = false,
  deleteConfirmId,
  onRenameConversation,
  editingTitleId,
  editingTitle,
  onEditingTitleChange,
  onStartEdit,
  onCancelEdit,
  onOpenDashboard,
  onOpenSettings,
  settingsActive = false,
}: SidebarProps) {
  const { user } = useUser()

  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [sidebarWidth, setSidebarWidth]         = useState(260)
  const [chatSectionOpen, setChatSectionOpen]   = useState(true)
  const [historyOpen, setHistoryOpen]           = useState(true)
  const [isResizing, setIsResizing]             = useState(false)
  const [devMode, setDevMode]                   = useState(false)
  const [convSearch, setConvSearch]             = useState('')
  const resizeWidthRef = useRef(260)

  // Stable ref so the [devMode] effect never captures a stale callback
  const onDevModeChangeRef = useRef(onDevModeChange)
  onDevModeChangeRef.current = onDevModeChange

  // Init from localStorage
  useEffect(() => {
    try {
      const s  = localStorage.getItem('md-sidebar')
      const sw = localStorage.getItem('md-sidebar-w')
      const cs = localStorage.getItem('md-chat-section')
      const hs = localStorage.getItem('md-history')
      const dm = localStorage.getItem('md-dev-mode')
      if (s)  setSidebarCollapsed(s === '1')
      if (sw) { const w = parseInt(sw, 10); setSidebarWidth(w); resizeWidthRef.current = w }
      if (cs) setChatSectionOpen(cs !== '0')
      if (hs && activePage === 'chat') setHistoryOpen(hs !== '0')
      if (dm) setDevMode(dm === '1')
    } catch { /* ignore */ }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    try { localStorage.setItem('md-sidebar', sidebarCollapsed ? '1' : '0') } catch { /* ignore */ }
  }, [sidebarCollapsed])

  useEffect(() => {
    try { localStorage.setItem('md-chat-section', chatSectionOpen ? '1' : '0') } catch { /* ignore */ }
  }, [chatSectionOpen])

  useEffect(() => {
    if (activePage === 'chat') {
      try { localStorage.setItem('md-history', historyOpen ? '1' : '0') } catch { /* ignore */ }
    }
  }, [historyOpen, activePage])

  useEffect(() => {
    try { localStorage.setItem('md-dev-mode', devMode ? '1' : '0') } catch { /* ignore */ }
    onDevModeChangeRef.current?.(devMode)
  }, [devMode])

  // Mobile focus trap
  useEffect(() => {
    if (!mobileNavOpen) return
    const sidebar = document.querySelector('.sidenav.mobile-open') as HTMLElement | null
    if (!sidebar) return

    const focusable = sidebar.querySelectorAll<HTMLElement>(
      'a, button, input, [tabindex]:not([tabindex="-1"])'
    )
    const first = focusable[0]
    const last  = focusable[focusable.length - 1]

    function trap(e: KeyboardEvent) {
      if (e.key !== 'Tab') return
      if (e.shiftKey) {
        if (document.activeElement === first) { e.preventDefault(); last.focus() }
      } else {
        if (document.activeElement === last) { e.preventDefault(); first.focus() }
      }
    }
    document.addEventListener('keydown', trap)
    first?.focus()
    return () => document.removeEventListener('keydown', trap)
  }, [mobileNavOpen])

  // ⌘B — collapse/expand sidebar
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'b') {
        e.preventDefault()
        setSidebarCollapsed(v => !v)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  function onResizeMouseDown(e: React.MouseEvent) {
    e.preventDefault()
    const startX = e.clientX
    const startW = sidebarWidth
    setIsResizing(true)

    function onMove(ev: globalThis.MouseEvent) {
      const newW = Math.max(180, Math.min(480, startW + ev.clientX - startX))
      setSidebarWidth(newW)
      resizeWidthRef.current = newW
    }
    function onUp() {
      setIsResizing(false)
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      try { localStorage.setItem('md-sidebar-w', String(resizeWidthRef.current)) } catch { /* ignore */ }
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

  const filteredConvs = convSearch.trim()
    ? conversations.filter(c => c.title.toLowerCase().includes(convSearch.toLowerCase()))
    : conversations

  const budgetPct   = todaySpend != null ? Math.min((todaySpend / dailyBudget) * 100, 100) : 0
  const budgetColor = budgetPct > 80 ? '#ef4444' : budgetPct > 50 ? '#f59e0b' : '#10b981'

  return (
    <nav
      className={`sidenav${sidebarCollapsed ? ' collapsed' : ''}${mobileNavOpen ? ' mobile-open' : ''}`}
      style={!sidebarCollapsed ? { width: sidebarWidth, minWidth: sidebarWidth } : {}}
      aria-label="Main navigation"
    >
      {/* Drag handle */}
      {!sidebarCollapsed && (
        <div
          className={`resize-handle${isResizing ? ' active' : ''}`}
          onMouseDown={onResizeMouseDown}
          title="Drag to resize"
        />
      )}

      {/* Logo row */}
      <div className="nav-top">
        <div className="nav-logo">
          <img src="/fronei-logo-wide.png" alt="Fronei" className="nav-logo-img" />
          <img src="/fronei-icon.svg" alt="Fronei" className="nav-logo-img-collapsed" />
        </div>
        <button
          className="sidebar-toggle-btn"
          onClick={() => setSidebarCollapsed(v => !v)}
          title={sidebarCollapsed ? 'Expand sidebar (⌘B)' : 'Collapse sidebar (⌘B)'}
          aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          <i className={`ti ${sidebarCollapsed ? 'ti-layout-sidebar-left-expand' : 'ti-layout-sidebar-left-collapse'}`} aria-hidden="true" />
        </button>
      </div>

      {/* ── Chat section ── */}
      <div className="nav-section grows">
        <div
          className="nav-section-hdr"
          onClick={() => {
            if (sidebarCollapsed) return
            const next = !chatSectionOpen
            setChatSectionOpen(next)
            if (next && activePage === 'chat') setHistoryOpen(true)
          }}
          title="Chat"
        >
          <i className="ti ti-message nav-section-icon" aria-hidden="true" />
          <span className="nav-section-label">Chat</span>
          <i className={`ti ti-chevron-down nav-section-chevron${chatSectionOpen ? ' open' : ''}`} aria-hidden="true" />
        </div>

        <div className={`nav-section-body${chatSectionOpen || sidebarCollapsed ? '' : ' closed'}`}>
          {activePage === 'chat' ? (
            <>
              <button className="nav-chat-cta" onClick={onNewConversation} title="New chat (⌘K)" aria-label="New chat">
                <i className="ti ti-plus" aria-hidden="true" />
                <span>New chat</span>
              </button>

              <div
                className="nav-sub-hdr"
                onClick={() => setHistoryOpen(v => !v)}
                title="Recent chats"
              >
                <span className="nav-sub-label">Recent</span>
                <i className={`ti ti-chevron-down nav-sub-chevron${historyOpen ? ' open' : ''}`} aria-hidden="true" />
              </div>

              <div className={`nav-section-body conv-section${historyOpen ? '' : ' closed'}`}>
                <div className="conv-search-wrap">
                  <input
                    className="conv-search-input"
                    placeholder="Search chats…"
                    value={convSearch}
                    onChange={e => setConvSearch(e.target.value)}
                    aria-label="Search conversations"
                  />
                </div>
                <div className="conv-list" role="list">
                  {filteredConvs.length === 0 && (
                    <p className="conv-empty">{convSearch ? 'No matches.' : 'No chats yet.'}</p>
                  )}
                  {filteredConvs.map(c => (
                    <div
                      key={c.id}
                      role="button"
                      tabIndex={0}
                      className={`conv-item${activeConvId === c.id ? ' active' : ''}`}
                      onClick={() => onLoadConversation?.(c.id)}
                      onKeyDown={e => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          onLoadConversation?.(c.id)
                        }
                      }}
                    >
                      <i className="ti ti-message-circle" aria-hidden="true" />
                      {editingTitleId === c.id ? (
                        <input
                          className="conv-rename-input"
                          value={editingTitle ?? ''}
                          onChange={e => onEditingTitleChange?.(e.target.value)}
                          onBlur={() => onRenameConversation?.(c.id, editingTitle ?? '')}
                          onKeyDown={e => {
                            if (e.key === 'Enter') onRenameConversation?.(c.id, editingTitle ?? '')
                            if (e.key === 'Escape') onCancelEdit?.()
                          }}
                          autoFocus
                          onClick={e => e.stopPropagation()}
                        />
                      ) : (
                        <span
                          className="conv-item-text"
                          onDoubleClick={e => { e.stopPropagation(); onStartEdit?.(c.id, c.title) }}
                        >{c.title}</span>
                      )}
                      {(c.total_cost_usd ?? 0) > 0 && (
                        <span className="conv-cost">${c.total_cost_usd!.toFixed(3)}</span>
                      )}
                      <div className="conv-item-actions">
                        <button className="conv-action-btn" onClick={e => onExport?.(e, c)} title="Export" aria-label="Export conversation">
                          <i className="ti ti-download" aria-hidden="true" />
                        </button>
                        <button
                          className={`conv-action-btn${deleteConfirmId === c.id ? ' danger-confirm' : ' danger'}`}
                          onClick={e => onDeleteConversation?.(e, c.id)}
                          title={deleteConfirmId === c.id ? 'Click again to confirm delete' : 'Delete'}
                          aria-label={deleteConfirmId === c.id ? 'Confirm delete' : 'Delete conversation'}
                        >
                          <i className={`ti ${deleteConfirmId === c.id ? 'ti-alert-triangle' : 'ti-trash'}`} aria-hidden="true" />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </>
          ) : (
            <>
              {onNewConversation ? (
                <button className="nav-chat-cta" type="button" onClick={onNewConversation}>
                  <i className="ti ti-plus" aria-hidden="true" />
                  <span>New chat</span>
                </button>
              ) : (
              <Link href="/" className="nav-chat-cta" style={{ textDecoration: 'none' }}>
                <i className="ti ti-plus" aria-hidden="true" />
                <span>New chat</span>
              </Link>
              )}
              <div className="nav-links">
                {onNewConversation ? (
                <button className="nav-link" type="button" onClick={onNewConversation}>
                  <i className="ti ti-message-circle" aria-hidden="true" />
                  <span>Open chat</span>
                </button>
                ) : (
                <Link href="/" className="nav-link">
                  <i className="ti ti-message-circle" aria-hidden="true" />
                  <span>Open chat</span>
                </Link>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      <div className="nav-divider" />

      {/* Dashboard link */}
      <div className="nav-links" style={{ flexShrink: 0 }}>
        {onOpenDashboard ? (
          <button
            type="button"
            className={`nav-link${activePage === 'dashboard' ? ' active' : ''}`}
            onClick={onOpenDashboard}
            aria-current={activePage === 'dashboard' ? 'page' : undefined}
          >
            <i className="ti ti-chart-bar" aria-hidden="true" />
            <span>Dashboard</span>
          </button>
        ) : activePage === 'chat' ? (
          <Link href="/dashboard" className="nav-link">
            <i className="ti ti-chart-bar" aria-hidden="true" />
            <span>Dashboard</span>
          </Link>
        ) : (
          <Link href="/dashboard" className="nav-link active" aria-current="page">
            <i className="ti ti-chart-bar" aria-hidden="true" />
            <span>Dashboard</span>
          </Link>
        )}
      </div>

      <div className="nav-divider" />

      {/* Settings live in the full-screen SettingsView opened from the footer gear. */}

      {/* Footer: user info + budget bar */}
      <div className="nav-footer">
        {user && (
          <div className="user-info">
            <div className="user-avatar">
              {(user.firstName?.[0] ?? user.primaryEmailAddress?.emailAddress?.[0] ?? '?').toUpperCase()}
            </div>
            <div className="user-details">
              <div className="user-name">{user.fullName ?? user.primaryEmailAddress?.emailAddress ?? 'You'}</div>
              {user.fullName && <div className="user-email">{user.primaryEmailAddress?.emailAddress}</div>}
            </div>
            {onOpenSettings && (
              <button
                className={`sign-out-btn${settingsActive ? ' active' : ''}`}
                title="Settings"
                aria-label="Open settings"
                onClick={onOpenSettings}
                type="button"
              >
                <i className="ti ti-settings" aria-hidden="true" />
              </button>
            )}
            <SignOutButton>
              <button className="sign-out-btn" title="Sign out" aria-label="Sign out">
                <i className="ti ti-logout" aria-hidden="true" />
              </button>
            </SignOutButton>
          </div>
        )}
        <div
          className="budget-row"
          title={todaySpend != null
            ? `$${todaySpend.toFixed(4)} of $${dailyBudget.toFixed(2)} used (${Math.round(budgetPct)}%)`
            : 'Spend data unavailable'}
        >
          <span>Today</span>
          <span className="budget-spend">
            {todaySpend != null ? `$${todaySpend.toFixed(4)}` : '—'}
          </span>
        </div>
        <div className="budget-track">
          <div className="budget-fill" style={{ width: `${budgetPct}%`, background: budgetColor }} />
        </div>
      </div>
    </nav>
  )
}

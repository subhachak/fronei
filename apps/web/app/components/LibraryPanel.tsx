'use client'

import { ChevronDown, Folder, Loader2, MessageSquare, Moon, Plus, Search, Sun, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { formatRelativeTime } from '../lib/format'
import type { PendingDelete, Workspace } from '../types'
import { AccountMenu } from './AccountMenu'
import { InlineDeleteConfirm } from './InlineDeleteConfirm'
import { SearchInput } from './ui/Field'

export function LibraryPanel({
  workspaces,
  workspacesLoading,
  workspaceAction,
  activeWorkspaceId,
  activeConversationId,
  onCreateWorkspace,
  onDeleteWorkspace,
  onCreateConversation,
  onDeleteConversation,
  onSelectConversation,
  expandedWorkspaceIds,
  editingWorkspaceId,
  editingWorkspaceName,
  onToggleWorkspace,
  onStartEditingWorkspace,
  onEditingWorkspaceNameChange,
  onSaveWorkspaceName,
  pendingDelete,
  onRequestDeleteWorkspace,
  onRequestDeleteConversation,
  onCancelDelete,
  isAdmin,
  view,
  onOpenProfile,
  onOpenAdmin,
  theme,
  onToggleTheme,
}: {
  workspaces: Workspace[]
  workspacesLoading: boolean
  workspaceAction: string
  activeWorkspaceId: string | null
  activeConversationId: string | null
  onCreateWorkspace: () => void
  onDeleteWorkspace: (workspaceId: string) => void
  onCreateConversation: (workspaceId: string) => void
  onDeleteConversation: (workspaceId: string, conversationId: string) => void
  onSelectConversation: (workspaceId: string, conversationId: string) => void
  expandedWorkspaceIds: Record<string, boolean>
  editingWorkspaceId: string | null
  editingWorkspaceName: string
  onToggleWorkspace: (workspaceId: string) => void
  onStartEditingWorkspace: (workspace: Workspace) => void
  onEditingWorkspaceNameChange: (value: string) => void
  onSaveWorkspaceName: (workspaceId: string) => void
  pendingDelete: PendingDelete
  onRequestDeleteWorkspace: (workspaceId: string) => void
  onRequestDeleteConversation: (workspaceId: string, conversationId: string) => void
  onCancelDelete: () => void
  isAdmin: boolean
  view: 'chat' | 'profile' | 'admin'
  onOpenProfile: () => void
  onOpenAdmin: () => void
  theme: 'light' | 'dark'
  onToggleTheme: () => void
}) {
  const [workspaceSearchOpen, setWorkspaceSearchOpen] = useState(false)
  const [workspacesTileOpen, setWorkspacesTileOpen] = useState(view === 'chat')
  const [workspaceSearch, setWorkspaceSearch] = useState('')
  const [conversationSearchOpen, setConversationSearchOpen] = useState<Record<string, boolean>>({})
  const [conversationSearch, setConversationSearch] = useState<Record<string, string>>({})
  const workspaceQuery = workspaceSearch.trim().toLowerCase()
  const visibleWorkspaces = workspaces.filter(workspace => (
    !workspaceQuery
    || workspace.name.toLowerCase().includes(workspaceQuery)
    || workspace.conversations.some(conversation => conversation.title.toLowerCase().includes(workspaceQuery))
  ))

  useEffect(() => {
    setWorkspacesTileOpen(view === 'chat')
  }, [view])

  return (
    <div className="flex h-full flex-col">
      <div className="mb-4 flex items-center justify-between gap-3">
        <a
          href="/"
          aria-label="Go to Fronei home"
          title="Go to Fronei home"
          className="flex h-11 min-w-0 max-w-full items-center rounded-lg pr-2 transition-opacity hover:opacity-80"
        >
          <img src="/fronei-logo.svg" alt="Fronei" className="h-9 w-auto min-w-0 dark:hidden" />
          <img src="/fronei-logo-dark.svg" alt="Fronei" className="hidden h-9 w-auto min-w-0 dark:block" />
        </a>
        <button
          type="button"
          onClick={onToggleTheme}
          aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          title={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          className="grid h-8 w-8 flex-shrink-0 place-items-center rounded-full border border-neutral-200 text-neutral-600 hover:bg-neutral-100 dark:border-neutral-800 dark:text-neutral-300 dark:hover:bg-neutral-800"
        >
          {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
        </button>
      </div>
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <p className="text-[11px] font-bold uppercase tracking-wider text-neutral-400">Studio</p>
          <h1 className="mt-0.5 text-lg font-bold text-neutral-900 dark:text-neutral-50">Workspaces</h1>
        </div>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setWorkspaceSearchOpen(open => !open)}
            aria-label="Search workspaces"
            title="Search workspaces"
            className="grid h-8 w-8 place-items-center rounded-full border border-neutral-200 text-neutral-500 dark:border-neutral-800 dark:text-neutral-400"
          >
            <Search size={14} />
          </button>
          <button
            type="button"
            onClick={onCreateWorkspace}
            aria-label="Create workspace"
            title="Create workspace"
            className="grid h-8 w-8 place-items-center rounded-full bg-neutral-900 text-white dark:bg-white dark:text-neutral-900"
          >
            <Plus size={15} />
          </button>
        </div>
      </div>

      {workspaceSearchOpen && (
        <SearchInput value={workspaceSearch} onChange={setWorkspaceSearch} onClear={() => setWorkspaceSearch('')} placeholder="Search workspaces..." />
      )}

      <div className="min-h-0 flex-1 space-y-2.5 overflow-y-auto">
        <details
          open={workspacesTileOpen}
          onToggle={event => setWorkspacesTileOpen(event.currentTarget.open)}
          className="overflow-hidden rounded-xl border border-neutral-200 bg-white/80 dark:border-neutral-800 dark:bg-neutral-900/60"
        >
          <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-3">
            <span className="flex min-w-0 items-center gap-2">
              <Folder size={15} className="flex-shrink-0 text-neutral-400" />
              <span className="truncate text-sm font-bold text-neutral-900 dark:text-neutral-50">Workspaces</span>
            </span>
            <span className="flex flex-shrink-0 items-center gap-2">
              {workspacesLoading ? <Loader2 size={13} className="animate-spin text-neutral-400" /> : <span className="text-xs font-semibold text-neutral-400">{workspaces.length}</span>}
              <ChevronDown size={15} className="text-neutral-400" />
            </span>
          </summary>
          <div className="grid gap-2.5 border-t border-neutral-100 p-2.5 dark:border-neutral-800">
            {workspaceAction && (
              <div className="flex items-center gap-2 rounded-lg border border-neutral-200 bg-neutral-50 px-3 py-2 text-xs font-bold text-neutral-500 dark:border-neutral-800 dark:bg-neutral-950 dark:text-neutral-400">
                <Loader2 size={13} className="animate-spin" />
                <span className="truncate">{workspaceAction}</span>
              </div>
            )}
            {workspacesLoading && workspaces.length === 0 && <WorkspaceSkeleton />}
            {!workspacesLoading && workspaces.length === 0 && (
              <div className="rounded-lg border border-dashed border-neutral-300 p-5 text-sm text-neutral-500 dark:border-neutral-700 dark:text-neutral-400">
                Create a workspace to begin.
              </div>
            )}
            {workspaces.length > 0 && visibleWorkspaces.length === 0 && (
              <div className="rounded-lg border border-dashed border-neutral-300 p-5 text-sm text-neutral-500 dark:border-neutral-700 dark:text-neutral-400">
                No matching workspaces.
              </div>
            )}
            {visibleWorkspaces.map((workspace, index) => {
              const isActive = workspace.id === activeWorkspaceId
              const expanded = expandedWorkspaceIds[workspace.id] ?? (isActive || index === 0)
              const conversationQuery = (conversationSearch[workspace.id] || '').trim().toLowerCase()
              const visibleConversations = workspace.conversations.filter(conversation => (
                !conversationQuery
                || conversation.title.toLowerCase().includes(conversationQuery)
                || String(conversation.turnCount || conversation.turns.length).includes(conversationQuery)
              ))
              const turnCount = workspace.conversations.reduce((total, conversation) => total + (conversation.turnCount || conversation.turns.length), 0)

              return (
                <section
                  key={workspace.id}
                  className={`overflow-hidden rounded-xl border ${isActive ? 'border-neutral-300 bg-white dark:border-neutral-600 dark:bg-neutral-900' : 'border-neutral-200 bg-white/70 dark:border-neutral-800 dark:bg-neutral-900/50'}`}
                >
                  <div className="grid grid-cols-[28px_minmax(0,1fr)_auto] items-center gap-2 p-3">
                    <button
                      type="button"
                      onClick={() => onToggleWorkspace(workspace.id)}
                      aria-label={expanded ? 'Collapse workspace' : 'Expand workspace'}
                      title={expanded ? 'Collapse workspace' : 'Expand workspace'}
                      className="grid h-7 w-7 place-items-center rounded-full text-neutral-500 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800"
                    >
                      <ChevronDown size={15} className={expanded ? '' : '-rotate-90'} />
                    </button>
                    <div
                      onClick={() => onStartEditingWorkspace(workspace)}
                      title="Rename workspace"
                      className="flex min-w-0 cursor-text items-center gap-2"
                    >
                      <Folder size={14} className="flex-shrink-0 text-neutral-400" />
                      {editingWorkspaceId === workspace.id ? (
                        <input
                          value={editingWorkspaceName}
                          onChange={event => onEditingWorkspaceNameChange(event.target.value)}
                          onBlur={() => onSaveWorkspaceName(workspace.id)}
                          onKeyDown={event => {
                            if (event.key === 'Enter' || event.key === 'Escape') event.currentTarget.blur()
                          }}
                          onClick={event => event.stopPropagation()}
                          autoFocus
                          className="w-full min-w-0 rounded-md border border-neutral-300 bg-neutral-50 px-2 py-1 text-sm font-bold text-neutral-900 outline-none dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-50"
                        />
                      ) : (
                        <span className="truncate text-sm font-bold text-neutral-900 dark:text-neutral-50">{workspace.name}</span>
                      )}
                    </div>
                    <div className="flex items-center gap-0.5">
                      <button type="button" onClick={() => onCreateConversation(workspace.id)} aria-label="New conversation" title="New conversation" className="grid h-7 w-7 place-items-center rounded-full text-neutral-500 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800">
                        <MessageSquare size={14} />
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setConversationSearchOpen(prev => ({ ...prev, [workspace.id]: !prev[workspace.id] }))
                          if (!expanded) onToggleWorkspace(workspace.id)
                        }}
                        aria-label="Search conversations"
                        title="Search conversations"
                        className="grid h-7 w-7 place-items-center rounded-full text-neutral-500 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800"
                      >
                        <Search size={13} />
                      </button>
                      <button
                        type="button"
                        onClick={() => onRequestDeleteWorkspace(workspace.id)}
                        aria-label="Delete workspace"
                        title="Delete workspace"
                        disabled={workspaces.length <= 1}
                        className="grid h-7 w-7 place-items-center rounded-full text-neutral-500 hover:bg-neutral-100 disabled:opacity-30 dark:text-neutral-400 dark:hover:bg-neutral-800"
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                    <span className="col-span-2 col-start-2 -mt-1 text-xs text-neutral-400">
                      {workspace.isDraft ? 'Saving...' : `${workspace.conversations.length} conv · ${turnCount} turns`}
                    </span>
                  </div>

                  {pendingDelete?.type === 'workspace' && pendingDelete.workspaceId === workspace.id && (
                    <InlineDeleteConfirm
                      title="Delete workspace?"
                      description="All conversations and artifacts inside it will be removed."
                      onCancel={onCancelDelete}
                      onConfirm={() => onDeleteWorkspace(workspace.id)}
                    />
                  )}

                  {expanded && (
                    <div className="grid gap-1.5 border-t border-neutral-100 p-2.5 dark:border-neutral-800">
                      {conversationSearchOpen[workspace.id] && (
                        <SearchInput
                          value={conversationSearch[workspace.id] || ''}
                          onChange={value => setConversationSearch(prev => ({ ...prev, [workspace.id]: value }))}
                          onClear={() => setConversationSearch(prev => ({ ...prev, [workspace.id]: '' }))}
                          placeholder="Search conversations..."
                        />
                      )}
                      {visibleConversations.length === 0 && (
                        <div className="rounded-md border border-dashed border-neutral-200 p-2.5 text-xs font-semibold text-neutral-400 dark:border-neutral-700">
                          No matching conversations.
                        </div>
                      )}
                      {visibleConversations.map(conversation => {
                        const isConvActive = conversation.id === activeConversationId
                        return (
                          <div key={conversation.id} className={`overflow-hidden rounded-lg border ${isConvActive ? 'border-neutral-900 bg-neutral-50 dark:border-neutral-100 dark:bg-neutral-800' : 'border-neutral-100 bg-neutral-50/60 dark:border-neutral-800 dark:bg-neutral-900/40'}`}>
                            <div className="grid grid-cols-[minmax(0,1fr)_32px] items-stretch">
                              <button
                                type="button"
                                onClick={() => onSelectConversation(workspace.id, conversation.id)}
                                className="flex min-w-0 items-center gap-2 px-2.5 py-2 text-left"
                              >
                                <MessageSquare size={14} className="flex-shrink-0 text-neutral-400" />
                                <span className="grid min-w-0 gap-0.5">
                                  <span className="truncate text-[13px] font-bold text-neutral-900 dark:text-neutral-50">{conversation.title}</span>
                                  <small className="truncate text-[11px] font-medium text-neutral-400">
                                    {conversation.isDraft ? 'Draft · not saved yet' : `${conversation.turnCount || conversation.turns.length} turns · ${formatRelativeTime(conversation.updatedAt)}`}
                                  </small>
                                </span>
                              </button>
                              <button
                                type="button"
                                onClick={() => onRequestDeleteConversation(workspace.id, conversation.id)}
                                aria-label="Delete conversation"
                                className="grid place-items-center border-l border-neutral-100 text-red-400 hover:bg-red-50 dark:border-neutral-800 dark:hover:bg-red-500/10"
                              >
                                <Trash2 size={13} />
                              </button>
                            </div>
                            {pendingDelete?.type === 'conversation' && pendingDelete.conversationId === conversation.id && (
                              <InlineDeleteConfirm
                                title="Delete conversation?"
                                description="This removes the chat turns and any generated artifacts."
                                onCancel={onCancelDelete}
                                onConfirm={() => onDeleteConversation(workspace.id, conversation.id)}
                              />
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </section>
              )
            })}
          </div>
        </details>
      </div>

      <AccountMenu isAdmin={isAdmin} onOpenProfile={onOpenProfile} onOpenAdmin={onOpenAdmin} />
    </div>
  )
}

function WorkspaceSkeleton() {
  return (
    <div className="grid gap-2.5">
      {[0, 1, 2].map(index => (
        <div key={index} className="rounded-xl border border-neutral-200 bg-white/70 p-3 dark:border-neutral-800 dark:bg-neutral-900/50">
          <div className="flex items-center gap-2">
            <div className="h-7 w-7 animate-pulse rounded-full bg-neutral-200 dark:bg-neutral-800" />
            <div className="h-3.5 min-w-0 flex-1 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
            <div className="h-7 w-16 animate-pulse rounded-full bg-neutral-200 dark:bg-neutral-800" />
          </div>
          <div className="ml-9 mt-2 h-3 w-28 animate-pulse rounded bg-neutral-200 dark:bg-neutral-800" />
        </div>
      ))}
    </div>
  )
}

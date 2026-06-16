'use client'

import { SignOutButton, useUser } from '@clerk/nextjs'
import {
  IconLayoutSidebarLeftCollapse,
  IconLayoutSidebarLeftExpand,
  IconLogout,
  IconMessage,
  IconPlus,
  IconSearch,
} from '@tabler/icons-react'
import { useMemo, useState } from 'react'
import { Button } from './ui/button'
import { Input } from './ui/input'
import { cn } from '@/v2/lib/utils'

export type ConversationSummary = {
  id: string
  title: string
  updated_at: string
  total_cost_usd?: number
}

interface ConversationSidebarProps {
  collapsed: boolean
  conversations: ConversationSummary[]
  activeConvId: string | null
  onSelectConversation: (id: string) => void
  onNewConversation: () => void
  onToggleCollapse: () => void
}

export function ConversationSidebar({
  collapsed,
  conversations,
  activeConvId,
  onSelectConversation,
  onNewConversation,
  onToggleCollapse,
}: ConversationSidebarProps) {
  const { user } = useUser()
  const [query, setQuery] = useState('')

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return conversations
    return conversations.filter(c => c.title.toLowerCase().includes(needle))
  }, [conversations, query])

  return (
    <div className={cn('flex h-full flex-col', collapsed && 'items-center')} data-collapsed={collapsed}>
      <div className="flex h-14 items-center gap-2 px-2">
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center justify-center rounded-md p-1 hover:bg-accent"
          onClick={onNewConversation}
          aria-label="New chat"
        >
          <img src={collapsed ? '/fronei-icon.svg' : '/fronei-logo-wide.png'} alt="Fronei" className={collapsed ? 'h-7 w-7' : 'h-8 w-auto max-w-32'} />
        </button>
        {!collapsed && (
          <Button
            variant="ghost"
            size="icon"
            type="button"
            aria-label="Collapse sidebar"
            title="Collapse sidebar"
            onClick={onToggleCollapse}
          >
            <IconLayoutSidebarLeftCollapse className="h-4 w-4" />
          </Button>
        )}
      </div>

      <div className={cn('space-y-3 px-2', collapsed && 'w-full')}>
        <Button className={cn('w-full', collapsed && 'px-0')} type="button" onClick={onNewConversation} title="New chat">
          <IconPlus className="h-4 w-4" />
          {!collapsed && <span>New chat</span>}
        </Button>
        {collapsed ? (
          <Button variant="ghost" size="icon" type="button" aria-label="Expand sidebar" title="Expand sidebar" onClick={onToggleCollapse}>
            <IconLayoutSidebarLeftExpand className="h-4 w-4" />
          </Button>
        ) : (
          <div className="relative">
            <IconSearch className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input className="pl-8" placeholder="Search chats" value={query} onChange={e => setQuery(e.target.value)} />
          </div>
        )}
      </div>

      <div className="mt-3 min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {!collapsed && filtered.length === 0 && (
          <p className="px-2 py-6 text-center text-sm text-muted-foreground">{query ? 'No matches.' : 'No chats yet.'}</p>
        )}
        <div className="space-y-1" role="list">
          {filtered.map(c => (
            <button
              key={c.id}
              type="button"
              className={cn(
                'flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm transition-colors hover:bg-accent',
                activeConvId === c.id && 'bg-accent text-accent-foreground',
                collapsed && 'justify-center px-0',
              )}
              onClick={() => onSelectConversation(c.id)}
              title={c.title}
            >
              <IconMessage className="h-4 w-4 flex-none text-muted-foreground" />
              {!collapsed && (
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{c.title || 'Untitled chat'}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {formatDate(c.updated_at)}
                    {typeof c.total_cost_usd === 'number' && c.total_cost_usd > 0 ? ` · $${c.total_cost_usd.toFixed(3)}` : ''}
                  </span>
                </span>
              )}
            </button>
          ))}
        </div>
      </div>

      <footer className="border-t border-border p-2">
        <div className={cn('flex items-center gap-2', collapsed && 'justify-center')}>
          {user?.imageUrl ? (
            <img src={user.imageUrl} alt="" className="h-8 w-8 rounded-full" />
          ) : (
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-secondary text-xs font-semibold">
              {(user?.firstName?.[0] || user?.emailAddresses[0]?.emailAddress[0] || 'U').toUpperCase()}
            </div>
          )}
          {!collapsed && (
            <>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{user?.fullName || 'Fronei user'}</p>
                <p className="truncate text-xs text-muted-foreground">{user?.primaryEmailAddress?.emailAddress}</p>
              </div>
              <SignOutButton>
                <Button variant="ghost" size="icon" type="button" aria-label="Sign out" title="Sign out">
                  <IconLogout className="h-4 w-4" />
                </Button>
              </SignOutButton>
            </>
          )}
        </div>
      </footer>
    </div>
  )
}

function formatDate(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' }).format(date)
}

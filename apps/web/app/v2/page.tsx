'use client'

import { InputBar } from './components/InputBar'
import { ConversationSidebar } from './components/ConversationSidebar'
import { MessageThread } from './components/MessageThread'
import { Shell } from './components/Shell'
import { useConversation } from './hooks/useConversation'

export default function V2Page() {
  const {
    conversations,
    activeConvId,
    messages,
    isLoading,
    error,
    loadConversation,
    newConversation,
    sendMessage,
  } = useConversation()

  return (
    <Shell
      sidebar={({ collapsed, onToggleCollapse }) => (
        <ConversationSidebar
          collapsed={collapsed}
          conversations={conversations}
          activeConvId={activeConvId}
          onSelectConversation={loadConversation}
          onNewConversation={newConversation}
          onToggleCollapse={onToggleCollapse}
        />
      )}
      conversation={(
        <section className="flex min-h-0 flex-1 flex-col bg-background">
          <header className="hidden h-14 items-center justify-between border-b border-border px-5 md:flex">
            <div>
              <h1 className="text-sm font-semibold">Conversation</h1>
              <p className="text-xs text-muted-foreground">{activeConvId ? 'Synced with your Fronei history' : 'New chat'}</p>
            </div>
            {error && <p className="max-w-md truncate text-sm text-destructive" role="alert">{error}</p>}
          </header>
          {error && <p className="border-b border-border px-4 py-2 text-sm text-destructive md:hidden" role="alert">{error}</p>}
          <MessageThread messages={messages} isLoading={isLoading} />
          <InputBar onSend={sendMessage} disabled={isLoading} />
        </section>
      )}
      workPane={<div className="h-full w-80" />}
      workPaneOpen={false}
    />
  )
}

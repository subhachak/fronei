'use client'

import { useAuth } from '@clerk/nextjs'
import { useCallback, useEffect, useState } from 'react'
import type { ConversationSummary } from '../components/ConversationSidebar'
import type { Message } from '../components/MessageThread'
import type { QualityMode } from '../components/InputBar'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000'

type ApiMessage = Omit<Message, 'id' | 'total_cost_usd'> & {
  id: string | number
  estimated_cost_usd?: number
  total_cost_usd?: number
}

type ConversationDetail = ConversationSummary & {
  messages: ApiMessage[]
}

const QUALITY_PROFILE: Record<QualityMode, 'cost_saver' | 'balanced' | 'best_quality'> = {
  draft: 'cost_saver',
  standard: 'balanced',
  executive: 'best_quality',
}

const QUALITY_LEGACY: Record<QualityMode, 'quick' | 'smart' | 'thorough'> = {
  draft: 'quick',
  standard: 'smart',
  executive: 'thorough',
}

export function useConversation() {
  const { getToken, isLoaded, isSignedIn } = useAuth()
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [activeConvId, setActiveConvId] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const apiFetch = useCallback(async (path: string, options: RequestInit = {}) => {
    const token = await getToken()
    return fetch(`${API_BASE}${path}`, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.headers as Record<string, string> | undefined),
      },
    })
  }, [getToken])

  const loadConversations = useCallback(async () => {
    if (!isLoaded || !isSignedIn) return
    const res = await apiFetch('/conversations')
    if (!res.ok) throw new Error('Could not load conversations')
    setConversations(await res.json())
  }, [apiFetch, isLoaded, isSignedIn])

  const loadConversation = useCallback(async (id: string) => {
    setActiveConvId(id)
    setMessages([])
    setError(null)
    const messagesRes = await apiFetch(`/conversations/${id}/messages`)
    if (messagesRes.ok) {
      setMessages(normalizeMessages(await messagesRes.json()))
      return
    }

    const detailRes = await apiFetch(`/conversations/${id}`)
    if (!detailRes.ok) throw new Error('Could not load conversation')
    const detail: ConversationDetail = await detailRes.json()
    setMessages(normalizeMessages(detail.messages))
  }, [apiFetch])

  const newConversation = useCallback(() => {
    setActiveConvId(null)
    setMessages([])
    setError(null)
  }, [])

  const sendMessage = useCallback(async (message: string, qualityMode: QualityMode) => {
    const tempUserId = `temp-user-${Date.now()}`
    const now = new Date().toISOString()
    setError(null)
    setIsLoading(true)
    setMessages(prev => [...prev, { id: tempUserId, role: 'user', content: message, created_at: now }])

    try {
      const res = await apiFetch('/conversations/chat', {
        method: 'POST',
        body: JSON.stringify({
          conversation_id: activeConvId,
          message,
          profile: QUALITY_PROFILE[qualityMode],
          quality: QUALITY_LEGACY[qualityMode],
          quality_mode: qualityMode,
          deep_research: qualityMode === 'executive',
          research_mode: qualityMode === 'executive' ? 'deep' : 'quick',
          web_search: false,
          output_mode: 'default',
        }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: 'Request failed' }))
        throw new Error(body.detail || 'Request failed')
      }
      const data = await res.json()
      const conversationId = data.conversation_id as string
      setActiveConvId(conversationId)
      setMessages(prev => [
        ...prev,
        {
          id: String(data.message_id),
          role: 'assistant',
          content: data.answer || '',
          created_at: new Date().toISOString(),
          total_cost_usd: data.estimated_cost_usd ?? undefined,
        },
      ])
      await loadConversations()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
      setMessages(prev => prev.filter(m => m.id !== tempUserId))
    } finally {
      setIsLoading(false)
    }
  }, [activeConvId, apiFetch, loadConversations])

  useEffect(() => {
    loadConversations().catch(err => setError(err instanceof Error ? err.message : 'Could not load conversations'))
  }, [loadConversations])

  return {
    conversations,
    activeConvId,
    messages,
    isLoading,
    error,
    loadConversation,
    newConversation,
    sendMessage,
  }
}

function normalizeMessages(messages: ApiMessage[]): Message[] {
  return messages.map(m => ({
    id: String(m.id),
    role: m.role,
    content: m.content,
    created_at: m.created_at,
    total_cost_usd: m.total_cost_usd ?? m.estimated_cost_usd,
  }))
}

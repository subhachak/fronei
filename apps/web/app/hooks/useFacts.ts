'use client'

import { useCallback, useEffect, useState } from 'react'
import { readErrorBody } from '../lib/api'

type AuthorizedFetch = (path: string, init?: RequestInit) => Promise<Response>

export type Fact = {
  id: string
  entity_id: string
  entity_type: string
  fact_key: string
  fact_value: string
  confidence: number
  source_conversation_id: string | null
  created_at: string | null
  updated_at: string | null
}

export type FactInput = {
  entity_id: string
  entity_type: string
  fact_key: string
  fact_value: string
  confidence: number
}

export function useFacts({
  authorizedFetch,
  enabled,
}: {
  authorizedFetch: AuthorizedFetch
  enabled: boolean
}) {
  const [facts, setFacts] = useState<Fact[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!enabled) {
      setFacts([])
      return
    }
    setLoading(true)
    setError(null)
    try {
      const response = await authorizedFetch('/api/facts')
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not load facts'))
      const data = await response.json() as Fact[]
      setFacts(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load facts')
    } finally {
      setLoading(false)
    }
  }, [authorizedFetch, enabled])

  useEffect(() => {
    void load()
  }, [load])

  const deleteFact = useCallback(async (entityId: string, factKey: string) => {
    if (!enabled) return
    setError(null)
    try {
      const response = await authorizedFetch(
        `/api/facts/${encodeURIComponent(entityId)}/${encodeURIComponent(factKey)}`,
        { method: 'DELETE' },
      )
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not delete fact'))
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not delete fact')
    }
  }, [authorizedFetch, enabled, load])

  const putFact = useCallback(async (fact: FactInput) => {
    if (!enabled) return
    setError(null)
    try {
      const response = await authorizedFetch('/api/facts', {
        method: 'PUT',
        body: JSON.stringify(fact),
      })
      if (!response.ok) throw new Error(await readErrorBody(response, 'Could not save fact'))
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save fact')
    }
  }, [authorizedFetch, enabled, load])

  return { facts, loading, error, deleteFact, putFact, reload: load }
}

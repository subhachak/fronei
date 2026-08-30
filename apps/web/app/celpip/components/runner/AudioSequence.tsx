'use client'

import { Loader2, Volume2 } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { AssetPayload } from '../../types'
import type { useCelpip } from '../../hooks/useCelpip'

type Api = ReturnType<typeof useCelpip>

/**
 * Plays a listening item's audio.
 *
 * The audio is stored per speaker turn (see services/celpip/assets.py), so this
 * preloads every turn as a blob before playback starts and then plays them
 * back to back. Preloading first is what makes it gapless -- fetching turn N+1
 * while turn N plays would leave an audible hole at every speaker change, and
 * a network stall mid-conversation would cost the learner the item.
 *
 * `maxPlays` reproduces the real test: audio plays once and does not replay.
 */
export function AudioSequence({
  api,
  assets,
  segmentIndex,
  maxPlays,
  autoPlay,
  onFinished,
}: {
  api: Api
  assets: AssetPayload
  segmentIndex?: number
  maxPlays: number
  autoPlay?: boolean
  onFinished?: () => void
}) {
  const clips = assets.audio.filter(
    a => a.status === 'ready' && (segmentIndex === undefined || a.segment_index === segmentIndex),
  )
  const [urls, setUrls] = useState<string[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [playing, setPlaying] = useState(false)
  const [plays, setPlays] = useState(0)
  const [current, setCurrent] = useState(0)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const startedRef = useRef(false)

  const preload = useCallback(async () => {
    if (urls || loading || clips.length === 0) return urls
    setLoading(true)
    setError('')
    try {
      const loaded = await Promise.all(
        clips.map(async clip => {
          const response = await api.authorizedFetch(`/admin/celpip/media/${clip.id}`)
          if (!response.ok) throw new Error('Audio could not be loaded.')
          return URL.createObjectURL(await response.blob())
        }),
      )
      setUrls(loaded)
      return loaded
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Audio could not be loaded.')
      return null
    } finally {
      setLoading(false)
    }
  }, [api, clips, loading, urls])

  useEffect(() => {
    return () => {
      urls?.forEach(url => URL.revokeObjectURL(url))
    }
  }, [urls])

  const play = useCallback(async () => {
    const loaded = urls ?? (await preload())
    if (!loaded || loaded.length === 0) return
    setPlays(count => count + 1)
    setCurrent(0)
    setPlaying(true)
  }, [preload, urls])

  useEffect(() => {
    if (autoPlay && !startedRef.current && clips.length > 0) {
      startedRef.current = true
      void play()
    }
  }, [autoPlay, clips.length, play])

  useEffect(() => {
    if (!playing || !urls) return
    const element = audioRef.current
    if (!element) return
    element.src = urls[current]
    element.play().catch(() => setError('Playback was blocked. Press play.'))
  }, [playing, current, urls])

  const handleEnded = () => {
    if (!urls) return
    if (current + 1 < urls.length) {
      // A short beat between speaker turns, so a conversation sounds like one.
      window.setTimeout(() => setCurrent(index => index + 1), 350)
    } else {
      setPlaying(false)
      onFinished?.()
    }
  }

  if (clips.length === 0) {
    return (
      <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-[13px] text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
        This item has no audio yet. Rebuild its assets from the Question Bank.
      </p>
    )
  }

  const exhausted = maxPlays > 0 && plays >= maxPlays

  return (
    <div className="rounded-lg border border-neutral-200 bg-neutral-50 p-3 dark:border-neutral-800 dark:bg-neutral-900">
      <audio ref={audioRef} onEnded={handleEnded} className="hidden" />
      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={playing || loading || exhausted}
          onClick={() => void play()}
          className="inline-flex items-center gap-1.5 rounded-lg bg-neutral-900 px-3 py-2 text-sm font-semibold text-white disabled:opacity-40 dark:bg-white dark:text-neutral-900"
        >
          {loading ? <Loader2 size={14} className="animate-spin" /> : <Volume2 size={14} />}
          {loading ? 'Loading audio…' : playing ? 'Playing…' : plays === 0 ? 'Play audio' : 'Play again'}
        </button>
        <div className="min-w-0 text-[12px] text-neutral-500">
          {playing ? (
            <span>Turn {current + 1} of {urls?.length ?? clips.length}</span>
          ) : exhausted ? (
            <span>Audio plays once, as in the real test.</span>
          ) : (
            <span>{clips.length} turn(s){maxPlays > 0 ? ` · ${maxPlays - plays} play(s) left` : ''}</span>
          )}
        </div>
      </div>
      {error && <p className="mt-2 text-[12px] text-rose-600">{error}</p>}
    </div>
  )
}

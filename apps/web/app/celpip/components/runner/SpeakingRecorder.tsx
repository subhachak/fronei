'use client'

import { Check, Circle, Loader2, Mic, Square } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { formatClock } from '../ui'

type Phase = 'idle' | 'preparing' | 'recording' | 'uploading' | 'done' | 'error'

/**
 * Preparation timer, then a fixed recording window, then upload.
 *
 * The recording window is not stoppable early in simulation mode, matching the
 * real test: the microphone stays open for the full time whether or not the
 * candidate has finished. Outside simulation, stopping early and retaking are
 * both allowed, because a drill you cannot repeat is a bad drill.
 */
export function SpeakingRecorder({
  prepSeconds,
  responseSeconds,
  allowRetake,
  alreadyRecorded,
  onUpload,
}: {
  prepSeconds: number
  responseSeconds: number
  allowRetake: boolean
  alreadyRecorded: boolean
  onUpload: (blob: Blob, durationSeconds: number) => Promise<void>
}) {
  const [phase, setPhase] = useState<Phase>(alreadyRecorded ? 'done' : 'idle')
  const [remaining, setRemaining] = useState(0)
  const [error, setError] = useState('')
  const [level, setLevel] = useState(0)

  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const streamRef = useRef<MediaStream | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const rafRef = useRef<number | null>(null)
  const startedAtRef = useRef<number>(0)

  const cleanup = useCallback(() => {
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current)
    rafRef.current = null
    analyserRef.current = null
    streamRef.current?.getTracks().forEach(track => track.stop())
    streamRef.current = null
  }, [])

  useEffect(() => cleanup, [cleanup])

  const stopRecording = useCallback(() => {
    if (recorderRef.current && recorderRef.current.state === 'recording') {
      recorderRef.current.stop()
    }
  }, [])

  // One countdown drives both phases; hitting zero either starts the recording
  // or ends it.
  useEffect(() => {
    if (phase !== 'preparing' && phase !== 'recording') return
    if (remaining <= 0) {
      if (phase === 'preparing') void beginRecording()
      else stopRecording()
      return
    }
    const id = window.setInterval(() => setRemaining(value => value - 1), 1000)
    return () => window.clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, remaining])

  const beginRecording = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream

      const context = new AudioContext()
      const analyser = context.createAnalyser()
      analyser.fftSize = 512
      context.createMediaStreamSource(stream).connect(analyser)
      analyserRef.current = analyser
      const data = new Uint8Array(analyser.frequencyBinCount)
      const tick = () => {
        if (!analyserRef.current) return
        analyserRef.current.getByteTimeDomainData(data)
        let peak = 0
        for (const value of data) peak = Math.max(peak, Math.abs(value - 128))
        setLevel(Math.min(1, peak / 64))
        rafRef.current = requestAnimationFrame(tick)
      }
      tick()

      const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : MediaRecorder.isTypeSupported('audio/webm')
          ? 'audio/webm'
          : ''
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      chunksRef.current = []
      recorder.ondataavailable = event => {
        if (event.data.size > 0) chunksRef.current.push(event.data)
      }
      recorder.onstop = async () => {
        const seconds = (Date.now() - startedAtRef.current) / 1000
        cleanup()
        setPhase('uploading')
        try {
          await onUpload(new Blob(chunksRef.current, { type: mimeType || 'audio/webm' }), seconds)
          setPhase('done')
        } catch (err) {
          setError(err instanceof Error ? err.message : 'Upload failed.')
          setPhase('error')
        }
      }
      recorderRef.current = recorder
      startedAtRef.current = Date.now()
      recorder.start()
      setPhase('recording')
      setRemaining(responseSeconds)
    } catch (err) {
      setError(
        err instanceof Error && err.name === 'NotAllowedError'
          ? 'Microphone access was refused. Allow it in your browser, then try again.'
          : 'Could not start recording.',
      )
      setPhase('error')
    }
  }, [cleanup, onUpload, responseSeconds])

  if (phase === 'done') {
    return (
      <div className="flex items-center gap-3 rounded-lg border border-emerald-200 bg-emerald-50 p-3 dark:border-emerald-900 dark:bg-emerald-950/40">
        <Check size={16} className="text-emerald-600" />
        <span className="text-[13px] font-medium text-emerald-900 dark:text-emerald-300">
          Response recorded. It is transcribed and scored after you submit.
        </span>
        {allowRetake && (
          <button
            type="button"
            className="ml-auto text-[13px] font-semibold text-emerald-900 underline dark:text-emerald-300"
            onClick={() => { setPhase('idle'); setError('') }}
          >
            Record again
          </button>
        )}
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-neutral-200 bg-neutral-50 p-4 dark:border-neutral-800 dark:bg-neutral-900">
      {phase === 'idle' && (
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => { setPhase('preparing'); setRemaining(prepSeconds) }}
            className="inline-flex items-center gap-1.5 rounded-lg bg-neutral-900 px-3.5 py-2 text-sm font-semibold text-white dark:bg-white dark:text-neutral-900"
          >
            <Mic size={14} /> Start ({prepSeconds}s to prepare, then {responseSeconds}s to speak)
          </button>
          <p className="text-[12px] text-neutral-500">
            Recording starts automatically when preparation ends.
          </p>
        </div>
      )}

      {phase === 'preparing' && (
        <div className="space-y-2">
          <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">
            Preparing — {formatClock(remaining)}
          </p>
          <p className="text-[13px] text-neutral-500">
            Note three or four words, not sentences. Recording starts on its own.
          </p>
          <button
            type="button"
            onClick={() => setRemaining(0)}
            className="text-[13px] font-semibold text-neutral-700 underline dark:text-neutral-300"
          >
            I&apos;m ready — start recording now
          </button>
        </div>
      )}

      {phase === 'recording' && (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <Circle size={10} className="animate-pulse fill-rose-500 text-rose-500" />
            <span className="text-sm font-bold text-neutral-900 dark:text-neutral-50">
              Recording — {formatClock(remaining)}
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-neutral-200 dark:bg-neutral-800">
            <div
              className="h-full rounded-full bg-rose-500 transition-[width] duration-75"
              style={{ width: `${Math.round(level * 100)}%` }}
            />
          </div>
          {allowRetake ? (
            <button
              type="button"
              onClick={stopRecording}
              className="inline-flex items-center gap-1.5 rounded-lg border border-neutral-300 px-3 py-1.5 text-[13px] font-semibold text-neutral-700 dark:border-neutral-700 dark:text-neutral-200"
            >
              <Square size={12} /> Stop early
            </button>
          ) : (
            <p className="text-[12px] text-neutral-500">
              The microphone stays open for the full window, as in the real test.
            </p>
          )}
        </div>
      )}

      {phase === 'uploading' && (
        <p className="flex items-center gap-2 text-sm text-neutral-500">
          <Loader2 size={14} className="animate-spin" /> Saving your recording…
        </p>
      )}

      {phase === 'error' && (
        <div className="space-y-2">
          <p className="text-[13px] text-rose-600">{error}</p>
          <button
            type="button"
            onClick={() => { setPhase('idle'); setError('') }}
            className="text-[13px] font-semibold underline"
          >
            Try again
          </button>
        </div>
      )}
    </div>
  )
}

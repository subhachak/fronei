'use client'

import { AlertTriangle, Loader2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { PracticeMode, Profile, Skill, Spec } from '../types'
import type { useCelpip } from '../hooks/useCelpip'
import { BUTTON, BUTTON_QUIET, CARD, ErrorNote, SectionHeading } from './ui'

type Api = ReturnType<typeof useCelpip>
type Shortfall = { message: string; shortfalls: Record<string, number>; hint: string }

export function MockTestsView({ api, onStart }: { api: Api; onStart: (attemptId: string) => void }) {
  const [profile, setProfile] = useState<Profile | null>(null)
  const [spec, setSpec] = useState<Spec | null>(null)
  const [error, setError] = useState('')
  const [shortfall, setShortfall] = useState<Shortfall | null>(null)
  const [busy, setBusy] = useState('')
  const [micOk, setMicOk] = useState<boolean | null>(null)

  useEffect(() => {
    Promise.all([
      api.getJson<Profile>('/admin/celpip/profile'),
      api.getJson<Spec>('/admin/celpip/spec'),
    ])
      .then(([p, s]) => {
        setProfile(p)
        setSpec(s)
      })
      .catch(err => setError(err instanceof Error ? err.message : 'Could not load.'))
  }, [api])

  const checkMic = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      stream.getTracks().forEach(track => track.stop())
      setMicOk(true)
    } catch {
      setMicOk(false)
    }
  }, [])

  const launch = useCallback(
    async (body: Record<string, unknown>, key: string) => {
      setBusy(key)
      setError('')
      setShortfall(null)
      try {
        const test = await api.postJson<{ attempt_id: string }>('/admin/celpip/tests', body)
        onStart(test.attempt_id)
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Could not build this test.'
        // The API returns exactly which task types the bank is short of, so the
        // failure names the fix instead of just refusing.
        try {
          const parsed = JSON.parse(message)
          if (parsed?.detail?.shortfalls) setShortfall(parsed.detail as Shortfall)
          else setError(message)
        } catch {
          setError(message)
        }
      } finally {
        setBusy('')
      }
    },
    [api, onStart],
  )

  if (!profile || !spec) {
    return (
      <div className="flex items-center justify-center gap-2 py-24 text-sm text-neutral-400">
        {error ? <ErrorNote message={error} /> : (<><Loader2 size={16} className="animate-spin" /> Loading…</>)}
      </div>
    )
  }

  const fullMode = profile.test_type === 'general_ls' ? 'full_ls' : 'full'
  const needsMic = profile.components.includes('speaking')

  return (
    <div className="space-y-6">
      {error && <ErrorNote message={error} />}
      {shortfall && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-[13px] text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
          <p className="flex items-center gap-1.5 font-semibold">
            <AlertTriangle size={14} /> Not enough items yet
          </p>
          <ul className="mt-1.5 list-disc pl-5">
            {Object.entries(shortfall.shortfalls).map(([task, count]) => (
              <li key={task}>
                {task} — generate {count} more
              </li>
            ))}
          </ul>
          <p className="mt-1.5">{shortfall.hint}</p>
        </div>
      )}

      {needsMic && (
        <div className={CARD}>
          <SectionHeading
            title="Microphone and audio check"
            hint="Run this before a full test. Discovering a dead microphone at task 5 costs the sitting."
          />
          <div className="flex items-center gap-3">
            <button type="button" className={BUTTON_QUIET} onClick={() => void checkMic()}>
              Test microphone
            </button>
            {micOk === true && <span className="text-[13px] font-semibold text-emerald-600">Microphone works.</span>}
            {micOk === false && (
              <span className="text-[13px] font-semibold text-rose-600">
                Blocked. Allow microphone access in your browser.
              </span>
            )}
          </div>
        </div>
      )}

      <div>
        <SectionHeading title="Full test" hint="Every component, in order, under exam conditions." />
        <div className="grid gap-3 sm:grid-cols-2">
          <div className={CARD}>
            <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">
              Full exam simulation
            </p>
            <p className="mt-1 text-[13px] leading-relaxed text-neutral-500">
              Strict timing, one audio play, no answer changes, results withheld until the whole thing is done.
              Occasionally includes unscored content, unmarked — as the real test does.
            </p>
            <button
              type="button"
              disabled={busy === 'sim'}
              onClick={() => void launch({ mode: fullMode, practice_mode: 'simulation' }, 'sim')}
              className={`${BUTTON} mt-3 w-full justify-center`}
            >
              {busy === 'sim' ? 'Building…' : 'Start simulation'}
            </button>
          </div>
          <div className={CARD}>
            <p className="text-sm font-bold text-neutral-900 dark:text-neutral-50">Full mock, timed</p>
            <p className="mt-1 text-[13px] leading-relaxed text-neutral-500">
              Same content and the same clock, but audio can be replayed once and answers stay editable within a
              section. Use this before you are ready for the strict version.
            </p>
            <button
              type="button"
              disabled={busy === 'timed'}
              onClick={() => void launch({ mode: fullMode, practice_mode: 'timed' }, 'timed')}
              className={`${BUTTON} mt-3 w-full justify-center`}
            >
              {busy === 'timed' ? 'Building…' : 'Start timed mock'}
            </button>
          </div>
        </div>
      </div>

      <div>
        <SectionHeading title="Single component" hint="One skill under its official section limit." />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {profile.components.map((skill: Skill) => (
            <div key={skill} className={CARD}>
              <p className="text-sm font-bold capitalize text-neutral-900 dark:text-neutral-50">{skill}</p>
              <p className="mt-0.5 text-[13px] text-neutral-500">
                ~{Math.round((spec.sections.find(s => s.skill === skill)?.limit_seconds ?? 0) / 60)} min
              </p>
              <button
                type="button"
                disabled={busy === skill}
                onClick={() =>
                  void launch(
                    { mode: 'component', practice_mode: 'timed' as PracticeMode, components: [skill] },
                    skill,
                  )
                }
                className={`${BUTTON_QUIET} mt-3 w-full justify-center`}
              >
                {busy === skill ? 'Building…' : 'Start'}
              </button>
            </div>
          ))}
        </div>
      </div>

      <div>
        <SectionHeading
          title="Diagnostic"
          hint="A shorter pass across every component, sized to give the study plan a starting point."
        />
        <button
          type="button"
          disabled={busy === 'diag'}
          onClick={() => void launch({ mode: 'diagnostic', practice_mode: 'timed' }, 'diag')}
          className={BUTTON}
        >
          {busy === 'diag' ? 'Building…' : 'Start diagnostic'}
        </button>
      </div>
    </div>
  )
}

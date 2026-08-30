'use client'

import { CheckCircle2, Clock3, Headphones, Loader2, Mic, ShieldCheck } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { PracticeMode, Profile, Skill, Spec } from '../types'
import type { useCelpip } from '../hooks/useCelpip'
import { prepareAndCreateTest } from '../lib/prepareTest'
import { BUTTON, BUTTON_QUIET, CARD, ErrorNote, SectionHeading } from './ui'

type Api = ReturnType<typeof useCelpip>

const DIAGNOSTIC_TASKS: Partial<Record<Skill, string[]>> = {
  listening: ['listening_problem_solving', 'listening_viewpoints'],
  reading: ['reading_correspondence', 'reading_viewpoints'],
  writing: ['writing_email'],
  speaking: ['speaking_advice', 'speaking_opinions'],
}

export function MockTestsView({ api, onStart }: { api: Api; onStart: (attemptId: string) => void }) {
  const [profile, setProfile] = useState<Profile | null>(null)
  const [spec, setSpec] = useState<Spec | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [progress, setProgress] = useState('')
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
      try {
        if (!profile || !spec) throw new Error('Test format is still loading.')
        const mode = String(body.mode)
        const selectedSkills = (body.components as Skill[] | undefined) ?? profile.components
        const taskKeys = mode === 'diagnostic'
          ? selectedSkills.flatMap(skill => DIAGNOSTIC_TASKS[skill] ?? [])
          : spec.sections
              .filter(section => selectedSkills.includes(section.skill))
              .flatMap(section => section.tasks.map(task => task.key))
        const test = await prepareAndCreateTest(
          api,
          body,
          taskKeys.map(taskKey => ({ taskKey })),
          setProgress,
        )
        onStart(test.attempt_id)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not build this test.')
      } finally {
        setBusy('')
        setProgress('')
      }
    },
    [api, onStart, profile, spec],
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
      {progress && <div className="flex items-center gap-2 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm font-medium text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300"><Loader2 size={15} className="animate-spin" /> {progress} You can leave this page; generation continues in the background.</div>}

      <div>
        <p className="text-xs font-bold uppercase tracking-[0.15em] text-amber-600 dark:text-amber-400">Exam training</p>
        <h2 className="mt-1 text-3xl font-bold tracking-tight text-neutral-950 dark:text-white">Build confidence under the real clock</h2>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-neutral-500">Use a full simulation when you can protect the entire sitting. Choose a component test when you need a shorter, focused check.</p>
      </div>

      <section className="overflow-hidden rounded-3xl bg-neutral-950 p-5 text-white sm:p-7">
        <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
          <div>
            <span className="rounded-full bg-amber-400 px-2.5 py-1 text-xs font-bold text-neutral-950">Recommended weekly</span>
            <h3 className="mt-3 text-2xl font-bold">Full exam simulation</h3>
            <p className="mt-2 max-w-2xl text-sm leading-relaxed text-neutral-300">Every component in order, strict timing, one audio play, no answer changes, and results only after submission.</p>
            <div className="mt-5 grid gap-2 text-sm text-neutral-200 sm:grid-cols-2">
              <span className="flex items-center gap-2"><Clock3 size={15} className="text-amber-300" /> About {profile.test_type === 'general_ls' ? '70 minutes' : '3 hours'}</span>
              <span className="flex items-center gap-2"><Headphones size={15} className="text-amber-300" /> Headphones recommended</span>
              <span className="flex items-center gap-2"><ShieldCheck size={15} className="text-amber-300" /> Progress saves automatically</span>
              <span className="flex items-center gap-2"><Mic size={15} className="text-amber-300" /> Microphone required</span>
            </div>
            <button type="button" disabled={busy === 'sim' || (needsMic && micOk !== true)} onClick={() => void launch({ mode: fullMode, practice_mode: 'simulation' }, 'sim')} className="mt-6 inline-flex min-h-12 items-center justify-center rounded-xl bg-amber-400 px-5 py-3 text-sm font-bold text-neutral-950 hover:bg-amber-300 disabled:cursor-not-allowed disabled:opacity-40">
              {busy === 'sim' ? 'Building simulation…' : needsMic && micOk !== true ? 'Complete device check first' : 'Start full simulation'}
            </button>
          </div>
          <div className="rounded-2xl border border-white/10 bg-white/[0.07] p-4">
            <p className="text-sm font-bold">Before you begin</p>
            <ul className="mt-3 space-y-2 text-sm text-neutral-300">
              <li className="flex items-center gap-2"><CheckCircle2 size={15} className="text-emerald-400" /> Quiet environment</li>
              <li className="flex items-center gap-2"><CheckCircle2 size={15} className="text-emerald-400" /> Enough uninterrupted time</li>
              <li className="flex items-center gap-2"><CheckCircle2 size={15} className="text-emerald-400" /> Stable internet connection</li>
            </ul>
            {needsMic && <button type="button" onClick={() => void checkMic()} className="mt-4 flex min-h-11 w-full items-center justify-center gap-2 rounded-xl border border-white/20 px-3 py-2 text-sm font-bold hover:bg-white/10"><Mic size={15} /> {micOk === true ? 'Microphone ready' : micOk === false ? 'Try microphone again' : 'Test microphone'}</button>}
            {micOk === false && <p className="mt-2 text-xs text-rose-300">Allow microphone access in your browser, then retry.</p>}
          </div>
        </div>
      </section>

      <div>
        <SectionHeading title="Other test options" hint="Build stamina gradually or measure one component at a time." />
        <div className="grid gap-3 lg:grid-cols-[1.2fr_2fr]">
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
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
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
      </div>

      <div className="rounded-2xl border border-dashed border-neutral-300 p-4 dark:border-neutral-700">
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

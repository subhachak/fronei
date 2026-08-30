'use client'

import { useState } from 'react'
import type { Profile } from '../types'
import { BUTTON, BUTTON_QUIET, CARD, INPUT, LABEL } from './ui'

// A short, opinionated list. The full weakness taxonomy is 28 tags; asking a
// learner to self-diagnose against all of them before they have sat anything
// produces noise. Measurement replaces these as soon as there is a scored
// attempt.
const SELF_REPORT = [
  { tag: 'idea_development', label: 'I run out of things to say or write' },
  { tag: 'organization', label: 'My answers feel disorganised' },
  { tag: 'vocabulary_range', label: 'I reuse the same words' },
  { tag: 'verb_tense', label: 'Grammar and tenses slip' },
  { tag: 'time_management', label: 'I run out of time' },
  { tag: 'detail_retrieval', label: 'I miss details in listening' },
  { tag: 'long_pauses', label: 'I pause a lot when speaking' },
  { tag: 'register_formality', label: 'I am unsure how formal to be' },
]

export function OnboardingCard({
  busy,
  onSave,
  onSkip,
}: {
  busy: boolean
  onSave: (profile: Partial<Profile>) => void
  onSkip: () => void
}) {
  const [testType, setTestType] = useState<'general' | 'general_ls'>('general')
  const [testDate, setTestDate] = useState('')
  const [target, setTarget] = useState(9)
  const [weekday, setWeekday] = useState(1.5)
  const [weekend, setWeekend] = useState(3)
  const [tags, setTags] = useState<string[]>([])

  const toggle = (tag: string) =>
    setTags(prev => (prev.includes(tag) ? prev.filter(t => t !== tag) : [...prev, tag]))

  return (
    <div className="mx-auto max-w-2xl">
      <h2 className="text-xl font-bold text-neutral-900 dark:text-neutral-50">Set up your preparation</h2>
      <p className="mt-1 text-[13px] leading-relaxed text-neutral-500">
        Five answers. They decide which components you practise, how much gets scheduled each day, and where
        the first diagnostic points. Everything here is editable later.
      </p>

      <div className={`${CARD} mt-5 space-y-5`}>
        <div>
          <span className={LABEL}>Which test are you sitting?</span>
          <div className="flex gap-2">
            {([
              ['general', 'CELPIP-General', 'All four skills — used for permanent residence'],
              ['general_ls', 'CELPIP-General LS', 'Listening & Speaking — used for citizenship'],
            ] as const).map(([value, label, hint]) => (
              <button
                key={value}
                type="button"
                onClick={() => setTestType(value)}
                className={`flex-1 rounded-lg border p-3 text-left transition-colors ${
                  testType === value
                    ? 'border-neutral-900 bg-neutral-50 dark:border-white dark:bg-neutral-800'
                    : 'border-neutral-200 dark:border-neutral-700'
                }`}
              >
                <span className="block text-sm font-semibold text-neutral-900 dark:text-neutral-50">{label}</span>
                <span className="mt-0.5 block text-[12px] leading-snug text-neutral-500">{hint}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label className={LABEL} htmlFor="celpip-date">Test date</label>
            <input
              id="celpip-date"
              type="date"
              className={INPUT}
              value={testDate}
              onChange={e => setTestDate(e.target.value)}
            />
          </div>
          <div>
            <label className={LABEL} htmlFor="celpip-target">Target level</label>
            <select
              id="celpip-target"
              className={INPUT}
              value={target}
              onChange={e => setTarget(Number(e.target.value))}
            >
              {[12, 11, 10, 9, 8, 7, 6, 5, 4].map(level => (
                <option key={level} value={level}>
                  CELPIP {level}
                  {level === 9 ? ' — max Express Entry points' : level === 7 ? ' — common EE minimum' : level === 5 ? ' — citizenship' : ''}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label className={LABEL} htmlFor="celpip-weekday">Hours per weekday</label>
            <input
              id="celpip-weekday"
              type="number" min={0} max={12} step={0.5}
              className={INPUT}
              value={weekday}
              onChange={e => setWeekday(Number(e.target.value))}
            />
          </div>
          <div>
            <label className={LABEL} htmlFor="celpip-weekend">Hours per weekend day</label>
            <input
              id="celpip-weekend"
              type="number" min={0} max={12} step={0.5}
              className={INPUT}
              value={weekend}
              onChange={e => setWeekend(Number(e.target.value))}
            />
          </div>
        </div>

        <div>
          <span className={LABEL}>What already worries you? (optional)</span>
          <p className="mb-2 text-[12px] text-neutral-500">
            Only used to seed the first plan. Measured results replace these as soon as you sit anything.
          </p>
          <div className="flex flex-wrap gap-1.5">
            {SELF_REPORT.map(item => (
              <button
                key={item.tag}
                type="button"
                onClick={() => toggle(item.tag)}
                className={`rounded-full border px-3 py-1 text-[12px] font-medium transition-colors ${
                  tags.includes(item.tag)
                    ? 'border-neutral-900 bg-neutral-900 text-white dark:border-white dark:bg-white dark:text-neutral-900'
                    : 'border-neutral-200 text-neutral-600 dark:border-neutral-700 dark:text-neutral-300'
                }`}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex items-center gap-2 pt-1">
          <button
            type="button"
            disabled={busy}
            className={BUTTON}
            onClick={() =>
              onSave({
                test_type: testType,
                test_date: testDate || null,
                target_level: target,
                weekday_hours: weekday,
                weekend_hours: weekend,
                self_reported_weaknesses: tags,
                onboarding_state: 'complete',
              })
            }
          >
            {busy ? 'Building your plan…' : 'Build my plan'}
          </button>
          <button type="button" disabled={busy} className={BUTTON_QUIET} onClick={onSkip}>
            Skip for now
          </button>
        </div>
      </div>
    </div>
  )
}

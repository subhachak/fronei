'use client'

import { useState } from 'react'
import { cn } from '../../lib/cn'

type CitationStatus = 'verified' | 'conflict' | 'stale'

interface Citation {
  id: string
  status: CitationStatus
  label: string
  source: string
  explanation: string
}

const CITATIONS: Citation[] = [
  {
    id: 'S1',
    status: 'verified',
    label: 'verified',
    source: 'Vendor SOC 2 Type II report, Section 4.2 (filed 2026-03-11)',
    explanation: 'This claim was matched word-for-word against the primary source document and confirmed by a second independent excerpt.',
  },
  {
    id: 'S2',
    status: 'conflict',
    label: 'conflict',
    source: 'Vendor security questionnaire vs. public breach disclosure (2026-01)',
    explanation: 'The vendor’s questionnaire response contradicts a separately sourced public disclosure. Fronei surfaces both rather than silently picking one.',
  },
  {
    id: 'S3',
    status: 'stale',
    label: 'stale',
    source: 'Third-party pen test summary (dated 2024-06-02)',
    explanation: 'This source is over 18 months old and the vendor’s attestation cadence suggests a newer report likely exists. Flagged for follow-up before it’s relied on.',
  },
]

const STATUS_STYLES: Record<CitationStatus, { marker: string; ring: string; dot: string }> = {
  verified: {
    marker: 'border-emerald-300 bg-emerald-50 text-emerald-800 hover:bg-emerald-100',
    ring: 'border-emerald-200 bg-emerald-50',
    dot: 'bg-emerald-600',
  },
  conflict: {
    marker: 'border-red-300 bg-red-50 text-red-800 hover:bg-red-100',
    ring: 'border-red-200 bg-red-50',
    dot: 'bg-red-600',
  },
  stale: {
    marker: 'border-slate-300 bg-slate-100 text-slate-700 hover:bg-slate-200',
    ring: 'border-slate-300 bg-slate-100',
    dot: 'bg-slate-500',
  },
}

function CitationMarker({ citation, active, onToggle }: { citation: Citation; active: boolean; onToggle: (id: string | null) => void }) {
  const styles = STATUS_STYLES[citation.status]
  return (
    <span className="relative inline-block">
      <button
        type="button"
        onClick={() => onToggle(active ? null : citation.id)}
        aria-expanded={active}
        className={cn(
          'font-[family-name:var(--font-marketing-mono)] mx-0.5 rounded border px-1.5 py-0.5 text-xs font-semibold align-super transition-colors',
          styles.marker,
        )}
      >
        [{citation.id}]
      </button>
      {active && (
        <span
          role="tooltip"
          className={cn(
            'absolute left-1/2 top-full z-10 mt-2 w-72 -translate-x-1/2 rounded-lg border p-3 text-left shadow-lg',
            styles.ring,
          )}
        >
          <span className="flex items-center gap-2">
            <span className={cn('h-1.5 w-1.5 rounded-full', styles.dot)} />
            <span className="font-[family-name:var(--font-marketing-mono)] text-[11px] font-bold uppercase tracking-wide text-stone-500">
              {citation.label}
            </span>
          </span>
          <span className="mt-1.5 block text-xs font-semibold text-stone-800">{citation.source}</span>
          <span className="mt-1 block text-xs leading-relaxed text-stone-600">{citation.explanation}</span>
        </span>
      )}
    </span>
  )
}

export function CitationDemo() {
  const [activeId, setActiveId] = useState<string | null>(null)
  const byId = Object.fromEntries(CITATIONS.map(c => [c.id, c]))

  return (
    <div
      id="demo"
      className="scroll-mt-24 rounded-2xl border border-stone-200 bg-white p-6 shadow-sm sm:p-8"
      onClick={event => {
        if (event.target === event.currentTarget) setActiveId(null)
      }}
    >
      <p className="font-[family-name:var(--font-marketing-mono)] text-[11px] font-bold uppercase tracking-wide text-stone-400">
        Sample research excerpt &middot; click a citation marker
      </p>
      <p className="mt-4 text-base leading-loose text-stone-800">
        The vendor maintains SOC 2 Type II compliance with continuous monitoring controls
        <CitationMarker citation={byId.S1} active={activeId === 'S1'} onToggle={setActiveId} />.
        Its incident disclosure record is contested: internal questionnaire responses assert
        no reportable incidents in the past 24 months, which conflicts with a public breach
        notice on file
        <CitationMarker citation={byId.S2} active={activeId === 'S2'} onToggle={setActiveId} />.
        The most recent penetration test on record predates the vendor&rsquo;s current
        infrastructure migration
        <CitationMarker citation={byId.S3} active={activeId === 'S3'} onToggle={setActiveId} />,
        and should not be treated as current evidence of network security posture.
      </p>
      <div className="mt-6 flex flex-wrap gap-4 border-t border-stone-100 pt-4 text-xs font-medium text-stone-500">
        <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-emerald-600" /> Verified against source</span>
        <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-red-600" /> Conflicting sources</span>
        <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-slate-500" /> Stale source</span>
      </div>
    </div>
  )
}

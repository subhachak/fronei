'use client'

import {
  ArrowLeft,
  BookOpen,
  ClipboardList,
  Ellipsis,
  Database,
  GraduationCap,
  Home,
  Loader2,
  Moon,
  ShieldAlert,
  Sun,
  Target,
  Trophy,
} from 'lucide-react'
import { useCallback, useState } from 'react'
import { useTheme } from '../../hooks/useTheme'
import { useCelpip } from '../hooks/useCelpip'
import { HomeView } from './HomeView'
import { LearnView } from './LearnView'
import { PracticeView } from './PracticeView'
import { MockTestsView } from './MockTestsView'
import { ResultsView } from './ResultsView'
import { StudyPlanView } from './StudyPlanView'
import { QuestionBankView } from './QuestionBankView'
import { SessionRunner } from './runner/SessionRunner'

type View = 'home' | 'learn' | 'practice' | 'mocks' | 'results' | 'plan' | 'bank'

const NAV: { id: View; label: string; icon: typeof Home }[] = [
  { id: 'home', label: 'Today', icon: Home },
  { id: 'learn', label: 'Learn', icon: BookOpen },
  { id: 'practice', label: 'Practice', icon: Target },
  { id: 'mocks', label: 'Mock Test', icon: GraduationCap },
  { id: 'results', label: 'Progress', icon: Trophy },
]

export function CelpipShell() {
  const api = useCelpip()
  const { theme, toggleTheme } = useTheme()
  const [view, setView] = useState<View>('home')
  // A running attempt takes over the whole screen. An exam simulation with a
  // navigation rail down the side is not an exam simulation.
  const [runningAttempt, setRunningAttempt] = useState<string | null>(null)
  const [openResult, setOpenResult] = useState<string | null>(null)
  const [moreOpen, setMoreOpen] = useState(false)

  const finishAttempt = useCallback((attemptId: string) => {
    setRunningAttempt(null)
    setOpenResult(attemptId)
    setView('results')
  }, [])

  const openAttempt = useCallback((attemptId: string) => setRunningAttempt(attemptId), [])

  if (api.access === 'checking') {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-sm font-medium text-neutral-400">
        <Loader2 size={16} className="animate-spin" /> Checking access…
      </div>
    )
  }

  if (api.access === 'denied') {
    return (
      <div className="flex h-full items-start justify-center bg-white dark:bg-neutral-950">
        <div className="mt-24 max-w-sm rounded-xl border border-neutral-200 bg-neutral-50 p-6 text-center dark:border-neutral-800 dark:bg-neutral-900">
          <ShieldAlert size={28} className="mx-auto text-neutral-400" />
          <h2 className="mt-3 text-base font-bold text-neutral-900 dark:text-neutral-50">
            Admin access required
          </h2>
          <p className="mt-1.5 text-sm leading-relaxed text-neutral-500">
            The CELPIP workspace is admin-only. Ask an existing admin to grant your account the admin role.
          </p>
          <a href="/app" className="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-neutral-900 px-4 py-2 text-sm font-semibold text-white dark:bg-white dark:text-neutral-900">
            <ArrowLeft size={14} /> Back to studio
          </a>
        </div>
      </div>
    )
  }

  if (runningAttempt) {
    return (
      <SessionRunner
        api={api}
        attemptId={runningAttempt}
        onExit={() => setRunningAttempt(null)}
        onFinished={finishAttempt}
      />
    )
  }

  return (
    <div className="flex h-full flex-col overflow-hidden bg-white dark:bg-neutral-950">
      <header className="flex-shrink-0 border-b border-neutral-200 bg-white/95 px-4 py-3 backdrop-blur dark:border-neutral-800 dark:bg-neutral-950/95 sm:px-6">
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <a
              href="/app"
              className="grid h-8 w-8 flex-shrink-0 place-items-center rounded-full border border-neutral-200 text-neutral-500 hover:bg-neutral-100 dark:border-neutral-800 dark:text-neutral-400 dark:hover:bg-neutral-800"
              aria-label="Back to studio"
              title="Back to studio"
            >
              <ArrowLeft size={15} />
            </a>
            <div>
              <p className="text-[10px] font-bold uppercase tracking-wider text-neutral-400">Fronei</p>
              <h1 className="text-lg font-bold text-neutral-900 dark:text-neutral-50">CELPIP</h1>
            </div>
          </div>
          <button
            type="button"
            onClick={toggleTheme}
            aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
            className="grid h-8 w-8 flex-shrink-0 place-items-center rounded-full border border-neutral-200 text-neutral-600 dark:border-neutral-800 dark:text-neutral-300"
          >
            {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
          </button>
        </div>

        <nav className="-mx-1 mt-3 hidden gap-1 overflow-visible pb-0.5 sm:flex">
          {NAV.map(item => (
            <button
              key={item.id}
              type="button"
              onClick={() => setView(item.id)}
              className={`flex flex-shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5 text-[13px] font-semibold transition-colors ${
                view === item.id
                  ? 'bg-neutral-900 text-white dark:bg-white dark:text-neutral-900'
                  : 'text-neutral-500 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800'
              }`}
            >
              <item.icon size={14} />
              {item.label}
            </button>
          ))}
          <div className="relative ml-auto">
            <button type="button" onClick={() => setMoreOpen(v => !v)} className="flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[13px] font-semibold text-neutral-500 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800">
              <Ellipsis size={14} /> More
            </button>
            {moreOpen && (
              <div className="absolute right-0 top-9 z-30 w-44 rounded-xl border border-neutral-200 bg-white p-1 shadow-xl dark:border-neutral-800 dark:bg-neutral-900">
                <button type="button" onClick={() => { setView('plan'); setMoreOpen(false) }} className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium hover:bg-neutral-100 dark:hover:bg-neutral-800"><ClipboardList size={14} /> Study plan</button>
                <button type="button" onClick={() => { setView('bank'); setMoreOpen(false) }} className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium hover:bg-neutral-100 dark:hover:bg-neutral-800"><Database size={14} /> Manage questions</button>
              </div>
            )}
          </div>
        </nav>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
        <div className="mx-auto w-full max-w-5xl">
          {view === 'home' && (
            <HomeView api={api} onOpenAttempt={openAttempt} onNavigate={setView} onOpenResult={id => { setOpenResult(id); setView('results') }} />
          )}
          {view === 'learn' && <LearnView api={api} />}
          {view === 'practice' && <PracticeView api={api} onStart={openAttempt} />}
          {view === 'mocks' && <MockTestsView api={api} onStart={openAttempt} />}
          {view === 'results' && (
            <ResultsView api={api} initialAttemptId={openResult} onClear={() => setOpenResult(null)} onStart={openAttempt} />
          )}
          {view === 'plan' && <StudyPlanView api={api} onStart={openAttempt} />}
          {view === 'bank' && <QuestionBankView api={api} />}
        </div>
      </div>
      <nav className="grid flex-shrink-0 grid-cols-5 border-t border-neutral-200 bg-white px-1 pb-[max(.35rem,env(safe-area-inset-bottom))] pt-1 dark:border-neutral-800 dark:bg-neutral-950 sm:hidden">
        {NAV.map(item => (
          <button key={item.id} type="button" onClick={() => setView(item.id)} className={`flex min-h-12 flex-col items-center justify-center gap-0.5 rounded-lg text-[10px] font-semibold ${view === item.id ? 'text-neutral-950 dark:text-white' : 'text-neutral-400'}`}>
            <item.icon size={18} /> {item.label}
          </button>
        ))}
      </nav>
    </div>
  )
}

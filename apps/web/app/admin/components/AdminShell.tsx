'use client'

import {
  Activity,
  ArrowLeft,
  Cpu,
  LayoutDashboard,
  Loader2,
  Moon,
  ServerCog,
  ShieldAlert,
  Sun,
  Users,
} from 'lucide-react'
import { useState } from 'react'
import { useTheme } from '../../hooks/useTheme'
import { useAdmin } from '../hooks/useAdmin'
import { ModelPolicyTab } from './ModelPolicyTab'
import { OverviewTab } from './OverviewTab'
import { SystemTab } from './SystemTab'
import { UsageTab } from './UsageTab'
import { UsersTab } from './UsersTab'

type AdminTab = 'overview' | 'users' | 'modelpolicy' | 'usage' | 'system'

const TABS: { id: AdminTab; label: string; icon: typeof LayoutDashboard }[] = [
  { id: 'overview', label: 'Overview', icon: LayoutDashboard },
  { id: 'users', label: 'Users', icon: Users },
  { id: 'modelpolicy', label: 'Model policy', icon: Cpu },
  { id: 'usage', label: 'Usage', icon: Activity },
  { id: 'system', label: 'System', icon: ServerCog },
]

export function AdminShell() {
  const { authorizedFetch, access } = useAdmin()
  const { theme, toggleTheme } = useTheme()
  const [tab, setTab] = useState<AdminTab>('overview')

  return (
    <div className="flex h-full flex-col overflow-hidden bg-white dark:bg-neutral-950">
      <header className="flex-shrink-0 border-b border-neutral-200 bg-white/95 px-4 py-3 backdrop-blur dark:border-neutral-800 dark:bg-neutral-950/95 sm:px-6">
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <a
              href="/"
              className="grid h-8 w-8 flex-shrink-0 place-items-center rounded-full border border-neutral-200 text-neutral-500 hover:bg-neutral-100 dark:border-neutral-800 dark:text-neutral-400 dark:hover:bg-neutral-800"
              aria-label="Back to studio"
              title="Back to studio"
            >
              <ArrowLeft size={15} />
            </a>
            <div>
              <p className="text-[10px] font-bold uppercase tracking-wider text-neutral-400">Fronei</p>
              <h1 className="text-lg font-bold text-neutral-900 dark:text-neutral-50">Admin</h1>
            </div>
          </div>
          <button
            type="button"
            onClick={toggleTheme}
            aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
            title={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
            className="grid h-8 w-8 flex-shrink-0 place-items-center rounded-full border border-neutral-200 text-neutral-600 dark:border-neutral-800 dark:text-neutral-300"
          >
            {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
          </button>
        </div>

        {access === 'granted' && (
          <nav className="-mx-1 mt-3 flex gap-1 overflow-x-auto pb-0.5">
            {TABS.map(item => (
              <button
                key={item.id}
                type="button"
                onClick={() => setTab(item.id)}
                className={`flex flex-shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5 text-[13px] font-semibold transition-colors ${
                  tab === item.id
                    ? 'bg-neutral-900 text-white dark:bg-white dark:text-neutral-900'
                    : 'text-neutral-500 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800'
                }`}
              >
                <item.icon size={14} />
                {item.label}
              </button>
            ))}
          </nav>
        )}
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
        {access === 'checking' && (
          <div className="flex items-center justify-center gap-2 py-20 text-sm font-medium text-neutral-400">
            <Loader2 size={16} className="animate-spin" /> Checking admin access…
          </div>
        )}

        {access === 'denied' && (
          <div className="mx-auto mt-16 max-w-sm rounded-xl border border-neutral-200 bg-neutral-50 p-6 text-center dark:border-neutral-800 dark:bg-neutral-900">
            <ShieldAlert size={28} className="mx-auto text-neutral-400" />
            <h2 className="mt-3 text-base font-bold text-neutral-900 dark:text-neutral-50">Admin access required</h2>
            <p className="mt-1.5 text-sm leading-relaxed text-neutral-500 dark:text-neutral-400">
              This account isn&apos;t on the admin allowlist. If you think that&apos;s wrong, ask an existing admin to grant your account the admin role.
            </p>
            <a
              href="/"
              className="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-neutral-900 px-4 py-2 text-sm font-semibold text-white dark:bg-white dark:text-neutral-900"
            >
              <ArrowLeft size={14} /> Back to studio
            </a>
          </div>
        )}

        {access === 'granted' && (
          <>
            {tab === 'overview' && <OverviewTab authorizedFetch={authorizedFetch} />}
            {tab === 'users' && <UsersTab authorizedFetch={authorizedFetch} />}
            {tab === 'modelpolicy' && <ModelPolicyTab authorizedFetch={authorizedFetch} />}
            {tab === 'usage' && <UsageTab authorizedFetch={authorizedFetch} />}
            {tab === 'system' && <SystemTab authorizedFetch={authorizedFetch} />}
          </>
        )}
      </div>
    </div>
  )
}

'use client'

import type { AuthorizedFetch } from '../types'
import { EvalHarnessTab } from './EvalHarnessTab'

export function EvalsTab({ authorizedFetch }: { authorizedFetch: AuthorizedFetch }) {
  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-base font-bold text-neutral-900 dark:text-neutral-50">Evals</h2>
        <p className="mt-0.5 text-xs text-neutral-500">
          Manage eval cases, trigger LangGraph runs, and review scoring history.
        </p>
      </div>

      <EvalHarnessTab authorizedFetch={authorizedFetch} />
    </div>
  )
}

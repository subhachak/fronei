import './agent-v3-theme.css'
import type { Metadata } from 'next'

export const metadata: Metadata = {
  title: 'Fronei — Agent v3',
}

export default function AgentV3Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="h-screen w-screen overflow-hidden bg-white text-neutral-900 antialiased dark:bg-neutral-950 dark:text-neutral-50">
      {children}
    </div>
  )
}

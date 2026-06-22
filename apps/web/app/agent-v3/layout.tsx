import './agent-v3-theme.css'
import type { Metadata } from 'next'

export const metadata: Metadata = {
  title: 'Fronei — Agent v3',
}

export default function AgentV3Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="h-dvh w-screen overflow-hidden bg-white text-neutral-900 antialiased dark:bg-neutral-950 dark:text-neutral-50">
      {/* Reads agent-v3's own theme key before first paint to avoid a flash of the wrong theme. */}
      <script
        dangerouslySetInnerHTML={{
          __html: `try{var t=localStorage.getItem('agent-v3-theme');if(t==='light'||t==='dark'){document.documentElement.setAttribute('data-theme',t);}else if(window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches){document.documentElement.setAttribute('data-theme','light');}}catch(e){}`,
        }}
      />
      {children}
    </div>
  )
}

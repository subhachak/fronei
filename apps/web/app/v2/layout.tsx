import '../globals.css'
import './v2-tokens.css'
import type { Metadata } from 'next'

export const metadata: Metadata = {
  title: 'Fronei v2',
}

export default function V2Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="h-screen w-screen overflow-hidden bg-background text-foreground">
      {children}
    </div>
  )
}

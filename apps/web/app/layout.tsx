import './globals.css'
import './agent-theme.css'
import 'highlight.js/styles/github-dark.css'
import { ClerkProvider } from '@clerk/nextjs'
import { assertNoProductionE2EBypass, e2eAuthBypassEnabled } from './lib/e2e'

export const metadata = {
  title: 'Fronei',
  description: 'Your AI personal assistant',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  assertNoProductionE2EBypass()
  const content = (
    <>
      {/* Reads localStorage before first paint to prevent theme flash */}
      <script
        dangerouslySetInnerHTML={{
          __html: `try{var t=localStorage.getItem('theme');if(t==='light'||t==='dark'){document.documentElement.setAttribute('data-theme',t);}else if(window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches){document.documentElement.setAttribute('data-theme','light');}}catch(e){}`,
        }}
      />
      {children}
    </>
  )

  return (
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <head>
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.31.0/dist/tabler-icons.min.css" />
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content" />
        <link rel="icon" href="/favicon.ico" sizes="any" />
        <link rel="icon" href="/favicon-32.png" type="image/png" sizes="32x32" />
        <link rel="apple-touch-icon" href="/apple-touch-icon.png" />
        <link rel="manifest" href="/manifest.json" />
        <meta name="theme-color" content="#7c3aed" />
        <meta name="apple-mobile-web-app-capable" content="yes" />
        <meta name="apple-mobile-web-app-title" content="Fronei" />
        <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
      </head>
      <body className="h-dvh w-screen overflow-hidden bg-white text-neutral-900 antialiased dark:bg-neutral-950 dark:text-neutral-50">
        {e2eAuthBypassEnabled() ? content : <ClerkProvider>{content}</ClerkProvider>}
      </body>
    </html>
  )
}
